"""单证裁决与解析的单元测试:不依赖来源服务,直接构造单证。"""
import json
import tempfile
import unittest
from email.message import EmailMessage

from collector.mime_parse import extract_documents, parse_message, validate_document, DocumentError
from collector.reconcile import (apply_document, content_hash, handoff_reference, plan_actions,
                                 refresh_reviews, shipment_view, stop_reference)
from collector.redact import mask_text
from collector.resolve import resolve_review
from collector.store import Store


def doc(doc_type, shipment='F-1', boxes=('B1',), version=1, carrier='C1'):
    return {'type': doc_type, 'carrier': carrier, 'shipment': shipment,
            'version': version, 'boxes': sorted(boxes)}


def mime_with(payload, sender='a@example.test', subject='x'):
    msg = EmailMessage()
    msg['From'], msg['Subject'], msg['Message-ID'] = sender, subject, '<m@x>'
    msg.set_content('body')
    msg.add_attachment(json.dumps(payload).encode(), maintype='application',
                       subtype='json', filename='documents.json')
    return msg.as_bytes()


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='collector-unit-')
        self.store = Store(self.dir)
        self.cycle = self.store.begin_cycle()

    def tearDown(self):
        self.store.close()

    def apply(self, d, msg='m1'):
        return apply_document(self.store, d, msg, 'att-' + msg, self.cycle)


class Parse(StoreCase):
    def test_validate_ok(self):
        d = validate_document({'type': 'release', 'carrier': ' C1 ', 'shipment': 'F-1',
                               'version': 2, 'boxes': ['B2', 'B1', 'B1']})
        self.assertEqual(d['boxes'], ['B1', 'B2'])
        self.assertEqual(d['carrier'], 'C1')

    def test_validate_rejects_bad_fields(self):
        base = {'type': 'release', 'carrier': 'C1', 'shipment': 'F-1', 'version': 1, 'boxes': ['B1']}
        for patch in [{'type': 'waybill'}, {'version': 0}, {'version': '2'}, {'boxes': []},
                      {'carrier': ''}, {'boxes': ['']}]:
            with self.assertRaises(DocumentError, msg=patch):
                validate_document({**base, **patch})
        with self.assertRaises(DocumentError):
            validate_document({'type': 'release'})

    def test_extract_documents_reports_failures(self):
        parsed = parse_message(mime_with({'not': 'a doc'}))
        documents, failures = extract_documents(parsed['attachments'])
        self.assertEqual(documents, [])
        self.assertEqual(len(failures), 1)
        self.assertIn('缺少字段', failures[0][0])

    def test_extract_documents_bad_json(self):
        msg = EmailMessage()
        msg['From'] = 'a@b.c'
        msg.set_content('x')
        msg.add_attachment(b'{broken', maintype='application', subtype='json', filename='d.json')
        documents, failures = extract_documents(parse_message(msg.as_bytes())['attachments'])
        self.assertEqual((documents, len(failures)), ([], 1))


class Adjudication(StoreCase):
    def test_hold_outranks_older_release_regardless_of_arrival_order(self):
        # 先收到扣留 v2,晚到的放行 v1 不能使其失效。
        self.apply(doc('hold', boxes=('B1',), version=2), 'm1')
        self.apply(doc('release', boxes=('B1',), version=1), 'm2')
        self.apply(doc('manifest', boxes=('B1',), version=1), 'm3')
        st = shipment_view(self.store, 'C1', 'F-1')['boxes']['B1']
        self.assertEqual((st['release_state'], st['release_version']), ('held', 2))

    def test_newer_release_lifts_hold(self):
        self.apply(doc('hold', boxes=('B1',), version=2))
        self.apply(doc('release', boxes=('B1',), version=3))
        self.apply(doc('manifest', boxes=('B1',), version=1))
        st = shipment_view(self.store, 'C1', 'F-1')['boxes']['B1']
        self.assertEqual(st['release_state'], 'released')

    def test_same_version_opposite_conclusions_blocked_and_reviewed(self):
        self.apply(doc('release', boxes=('B1',), version=2))
        self.apply(doc('hold', boxes=('B1',), version=2))
        self.apply(doc('manifest', boxes=('B1',), version=1))
        self.apply(doc('arrival', boxes=('B1',), version=1))
        refresh_reviews(self.store, self.cycle)
        st = shipment_view(self.store, 'C1', 'F-1')['boxes']['B1']
        self.assertEqual((st['release_state'], st['blocked']), ('conflict', True))
        kinds = {r['kind'] for r in self.store.reviews()}
        self.assertIn('release_hold_conflict', kinds)
        # 冲突箱即使已到货也不进入交接计划。
        self.assertEqual(plan_actions(self.store)['handoffs'], [])

    def test_same_version_conflict_resolved_by_choosing_content(self):
        self.apply(doc('manifest', boxes=('B1', 'B2'), version=1), 'm1')
        changed = doc('manifest', boxes=('B1',), version=1)
        self.apply(changed, 'm2')
        refresh_reviews(self.store, self.cycle)
        view = shipment_view(self.store, 'C1', 'F-1')
        self.assertTrue(view['manifest_conflict'])
        review = next(r for r in self.store.reviews() if r['kind'] == 'same_version_conflict')
        with self.assertRaises(ValueError):
            resolve_review(self.store, review['id'], None, self.cycle)
        resolve_review(self.store, review['id'], content_hash(changed), self.cycle)
        view = shipment_view(self.store, 'C1', 'F-1')
        self.assertFalse(view['manifest_conflict'])
        self.assertEqual(sorted(view['boxes']), ['B1'])

    def test_conflict_auto_resolves_when_newer_version_arrives(self):
        self.apply(doc('manifest', boxes=('B1',), version=1), 'm0')
        self.apply(doc('release', boxes=('B1',), version=2), 'm1')
        self.apply(doc('hold', boxes=('B1',), version=2), 'm2')
        refresh_reviews(self.store, self.cycle)
        self.assertEqual([r['kind'] for r in self.store.reviews()], ['release_hold_conflict'])
        self.apply(doc('hold', boxes=('B1',), version=3), 'm3')
        refresh_reviews(self.store, self.cycle + 1)
        self.assertEqual(self.store.reviews(), [])
        st = shipment_view(self.store, 'C1', 'F-1')['boxes']['B1']
        self.assertEqual((st['release_state'], st['release_version']), ('held', 3))

    def test_partial_release_leaves_others_missing(self):
        self.apply(doc('manifest', boxes=('B1', 'B2', 'B3'), version=1))
        self.apply(doc('release', boxes=('B1',), version=1))
        view = shipment_view(self.store, 'C1', 'F-1')
        self.assertEqual(view['boxes']['B1']['release_state'], 'released')
        self.assertEqual(view['boxes']['B2']['release_state'], 'none')

    def test_duplicate_document_not_double_counted(self):
        self.assertEqual(self.apply(doc('release'), 'm1'), 'applied')
        self.assertEqual(self.apply(doc('release'), 'm2'), 'duplicate')
        self.assertEqual(len(self.store.documents()), 1)

    def test_out_of_scope_box_reviewed_then_auto_resolved(self):
        self.apply(doc('manifest', boxes=('B1',), version=1), 'm1')
        self.apply(doc('release', boxes=('B9',), version=1), 'm2')
        refresh_reviews(self.store, self.cycle)
        kinds = {r['kind'] for r in self.store.reviews()}
        self.assertIn('box_out_of_scope', kinds)
        self.apply(doc('manifest', boxes=('B1', 'B9'), version=2), 'm3')
        refresh_reviews(self.store, self.cycle + 1)
        self.assertEqual(self.store.reviews(), [])

    def test_plan_handoff_and_stop(self):
        self.apply(doc('manifest', boxes=('B1', 'B2'), version=1), 'm1')
        self.apply(doc('release', boxes=('B1', 'B2'), version=1), 'm2')
        self.apply(doc('arrival', boxes=('B1',), version=1), 'm3')
        plan = plan_actions(self.store)
        self.assertEqual([h['box'] for h in plan['handoffs']], ['B1'])
        self.assertEqual(plan['stops'], [])
        # 交接后被扣留 → 计划停交。
        self.store.upsert_handoff(handoff_reference('C1', 'F-1', 'B1'), 'B1', 'C1', 'F-1',
                                  'handoffs-1', 'accepted', self.cycle)
        self.apply(doc('hold', boxes=('B1',), version=2), 'm4')
        plan = plan_actions(self.store)
        self.assertEqual([s['box'] for s in plan['stops']], ['B1'])
        self.assertIn('扣留', plan['stops'][0]['reason'])
        # 已停交的箱不自动重交。
        self.store.upsert_stop(stop_reference('C1', 'F-1', 'B1'),
                               handoff_reference('C1', 'F-1', 'B1'), 'C1', 'F-1', 'B1',
                               '扣留', 'pending', self.cycle)
        self.apply(doc('release', boxes=('B1',), version=3), 'm5')
        self.assertEqual(plan_actions(self.store)['handoffs'], [])


class Redact(unittest.TestCase):
    def test_mask_email_and_phone(self):
        self.assertEqual(mask_text('carrier@example.test'), 'c***@example.test')
        self.assertEqual(mask_text('联系 13800001234 确认'), '联系 138****34 确认')
        self.assertEqual(mask_text(None), '')
        self.assertEqual(mask_text('箱单 F-701'), '箱单 F-701')


class Process(StoreCase):
    """process_message 层面:主题/发件人不影响归属,坏件进待核。"""

    def process(self, raw, config=None, msg_hash='m-hash'):
        from collector.service import Config, process_message
        summary = {'skipped': [], 'documents': {}}
        config = config or Config(data_dir=self.dir)
        process_message(self.store, msg_hash, raw, self.cycle, config, summary)
        return summary

    def test_subject_never_decides_attribution(self):
        # 主题写"已放行",附件实际是扣留通知:以附件内容为准。
        self.apply(doc('manifest', boxes=('B1',), version=1))
        raw = mime_with(doc('hold', boxes=('B1',), version=2), subject='已放行,请查收')
        self.process(raw)
        st = shipment_view(self.store, 'C1', 'F-1')['boxes']['B1']
        self.assertEqual(st['release_state'], 'held')

    def test_unparseable_attachment_goes_to_review(self):
        msg = EmailMessage()
        msg['From'] = 'a@b.c'
        msg.set_content('x')
        msg.add_attachment(b'{broken', maintype='application', subtype='json', filename='d.json')
        self.process(msg.as_bytes())
        kinds = {r['kind'] for r in self.store.reviews()}
        self.assertIn('unparseable', kinds)

    def test_message_without_document_goes_to_review(self):
        msg = EmailMessage()
        msg['From'] = 'a@b.c'
        msg.set_content('没有任何附件')
        self.process(msg.as_bytes())
        kinds = {r['kind'] for r in self.store.reviews()}
        self.assertIn('no_document', kinds)

    def test_read_sender_whitelist_only_scopes_reading(self):
        from collector.service import Config
        config = Config(data_dir=self.dir, read_senders=('ok@example.test',))
        raw = mime_with(doc('manifest', boxes=('B1',), version=1), sender='other@example.test')
        summary = self.process(raw, config)
        self.assertEqual(summary['skipped'], ['m-hash'])
        self.assertEqual(self.store.documents(), [])


if __name__ == '__main__':
    unittest.main()
