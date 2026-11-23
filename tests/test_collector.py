"""采集业务集成测试:真实 IMAP 会话、UIDVALIDITY 重建、归档移动、交接与停交。

只通过 imaplib/HTTP 与来源交互,不读 fixtures 或服务内存;阶段推进用
检查人员接口(业务程序本身不调用)。
"""
from support import *
import tempfile

from collector.service import Config, run_cycle
from collector.store import Store
from collector.reconcile import shipment_view


class CollectorCase(SourceCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix='collector-test-')

    def config(self):
        return Config(imap_port=self.app.mail_port, desk=self.base, data_dir=self.dir)

    def cycle(self):
        with Store(self.dir) as store:
            return run_cycle(self.config(), store)

    def view(self, shipment='F-701'):
        with Store(self.dir) as store:
            return shipment_view(store, 'C1', shipment)


class StageFlow(CollectorCase):
    def test_stage0_handoff_only_arrived_and_released(self):
        summary = self.cycle()
        self.assertEqual(len(summary['new_messages']), 3)
        self.assertEqual(summary['errors'], [])
        # 只有 BX1 同时满足箱单内+已放行+已到货;BX2 未到货不得登记。
        self.assertEqual(summary['handoffs'], ['handoff:C1:F-701:BX1'])
        remote = self.value('/handoff')
        self.assertEqual(list(remote), ['handoff:C1:F-701:BX1'])
        self.assertEqual(remote['handoff:C1:F-701:BX1']['boxes'][0]['state'], 'accepted')
        evidence = remote['handoff:C1:F-701:BX1']['payload']['evidence']
        self.assertEqual({e['type'] for e in evidence}, {'manifest', 'release', 'arrival'})
        view = self.view()
        self.assertEqual(view['boxes']['BX2']['release_state'], 'released')
        self.assertFalse(view['boxes']['BX2']['arrived'])

    def test_stage1_rebuild_move_hold_and_forward(self):
        self.cycle()
        self.control(1)
        summary = self.cycle()
        # UIDVALIDITY 重建+归档移动后,只有扣留与转发两封是新原件。
        self.assertEqual(len(summary['new_messages']), 2)
        with Store(self.dir) as store:
            self.assertEqual(len(store.list_messages()), 5)
            self.assertEqual(len(store.documents()), 5)
            shared = [m for m in store.list_messages() if m['message_id'] == '<shared@carrier.test>']
            # 同一 Message-ID 出现在不同转发中,内容不同即两份原件。
            self.assertEqual(len(shared), 2)
            manifest = next(m for m in store.list_messages() if m['subject'] == '箱单 F-701')
            locs = {(l['folder'], l['uid'], l['uidvalidity']) for l in store.message_locations(manifest['hash'])}
            self.assertEqual(locs, {('INBOX', 101, 100), ('Archive', 601, 200)})
        view = self.view()
        # 扣留 v2 推翻放行 v1;旧邮件晚到不改变版本裁决。
        self.assertEqual(view['boxes']['BX1']['release_state'], 'held')
        self.assertEqual(view['boxes']['BX1']['release_version'], 2)
        # 已交接的 BX1 被扣留:发起停交并等待现场确认。
        stops = self.value('/stop')
        self.assertIn('stop:C1:F-701:BX1', stops)
        self.assertEqual(stops['stop:C1:F-701:BX1']['state'], 'pending')
        self.assertEqual(stops['stop:C1:F-701:BX1']['payload']['handoff_reference'],
                         'handoff:C1:F-701:BX1')
        # 转发夹带的旧批次单证归入旧批次,不污染当前批次。
        old = self.view('F-OLD')
        self.assertEqual(sorted(old['boxes']), ['OLD1'])
        self.assertEqual(old['boxes']['OLD1']['release_state'], 'none')
        self.assertEqual(self.value('/handoff').keys(), {'handoff:C1:F-701:BX1'})

    def test_stage2_stop_confirmed(self):
        self.cycle()
        self.control(1)
        self.cycle()
        self.control(2)
        self.cycle()
        with Store(self.dir) as store:
            stop = store.get_stop('stop:C1:F-701:BX1')
            self.assertEqual(stop['state'], 'confirmed')

    def test_rerun_is_idempotent(self):
        self.cycle()
        self.control(1)
        self.cycle()
        summary = self.cycle()
        self.assertEqual(summary['handoffs'], [])
        self.assertEqual(summary['stops'], [])
        self.assertEqual(len(self.value('/handoff')), 1)
        self.assertEqual(len(self.value('/stop')), 1)


class Reconnect(CollectorCase):
    def test_restart_with_same_state_does_not_rehandoff(self):
        self.cycle()
        summary = self.cycle()  # 新 Store 实例模拟断线重连
        self.assertEqual(summary['handoffs'], [])
        self.assertEqual(len(self.value('/handoff')), 1)

    def test_fresh_state_adopts_existing_handoff(self):
        self.cycle()
        other = tempfile.mkdtemp(prefix='collector-fresh-')
        config = Config(imap_port=self.app.mail_port, desk=self.base, data_dir=other)
        with Store(other) as store:
            summary = run_cycle(config, store)
            self.assertEqual(summary['handoffs'], [])
            hand = store.get_handoff('handoff:C1:F-701:BX1', 'BX1')
            self.assertEqual(hand['state'], 'accepted')
            self.assertEqual(hand['receipt'], 'handoffs-1')
        self.assertEqual(len(self.value('/handoff')), 1)

    def test_lost_response_reconciled_by_query(self):
        # 模拟上一轮提交已保存但响应丢失:交接台已有记录,本地无记录。
        self.request('/handoff', {'reference': 'handoff:C1:F-701:BX1', 'carrier': 'C1',
                                  'shipment': 'F-701', 'boxes': ['BX1'], 'evidence': [],
                                  'lose_response': True})
        summary = self.cycle()
        self.assertEqual(summary['handoffs'], [])
        self.assertEqual(len(self.value('/handoff')), 1)
        with Store(self.dir) as store:
            self.assertEqual(store.get_handoff('handoff:C1:F-701:BX1', 'BX1')['state'], 'accepted')

    def test_desk_reset_marks_lost_and_reposts(self):
        self.cycle()
        self.app.remote.clear()  # 模拟来源重启,交接记录清空
        summary = self.cycle()
        self.assertEqual(summary['handoffs'], ['handoff:C1:F-701:BX1'])
        self.assertEqual(len(self.value('/handoff')), 1)
