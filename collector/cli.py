"""操作查询与采集入口。所有查询输出经脱敏,不泄漏合成联系人信息。"""
import argparse
import sys
import time

from .reconcile import (handoff_reference, shipment_view, stop_reference)
from .redact import mask_text
from .service import Config, run_cycle
from .store import Store

RELEASE_LABEL = {'none': '无放行依据', 'released': '已放行', 'held': '已扣留', 'conflict': '同版冲突待核'}
HANDOFF_LABEL = {'accepted': '已交接', 'unknown': '提交结果未知', 'lost': '交接台查无记录', 'stopped': '已停交'}
STOP_LABEL = {'pending': '待现场确认', 'confirmed': '现场已确认', 'unknown': '提交结果未知', 'lost': '交接台查无记录'}


def _config(args):
    senders = tuple(s.strip() for s in (args.read_senders or '').split(',') if s.strip())
    return Config(imap_host=args.imap_host, imap_port=args.imap_port, imap_user=args.imap_user,
                  imap_password=args.imap_password, desk=args.desk, data_dir=args.data_dir,
                  read_senders=senders)


def _print(rows, columns):
    if not rows:
        print('(无记录)')
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ''))) for r in rows)) for c in columns}
    print('  '.join(c.ljust(widths[c]) for c in columns))
    for r in rows:
        print('  '.join(str(r.get(c, '')).ljust(widths[c]) for c in columns))


def cmd_collect(args):
    config = _config(args)
    rounds = args.cycles
    with Store(config.data_dir) as store:
        for i in range(rounds):
            summary = run_cycle(config, store)
            print('第 %(cycle)s 轮:新邮件 %(nm)d 封,交接 %(nh)d 笔,停交 %(ns)d 笔,错误 %(ne)d 个' % {
                'cycle': summary['cycle'], 'nm': len(summary['new_messages']),
                'nh': len(summary['handoffs']), 'ns': len(summary['stops']),
                'ne': len(summary['errors'])})
            for err in summary['errors']:
                print('  错误: ' + mask_text(err))
            if i + 1 < rounds:
                time.sleep(args.interval)
    return 0


def _box_rows(store, carrier, shipment):
    view = shipment_view(store, carrier, shipment)
    rows = []
    for box, st in view['boxes'].items():
        hand = store.get_handoff(handoff_reference(carrier, shipment, box), box)
        stop = store.get_stop(stop_reference(carrier, shipment, box))
        rows.append({'批次': shipment, '箱号': box, '箱单内': '是' if st['expected'] else '否',
                     '到货': '是' if st['arrived'] else '否',
                     '放行状态': RELEASE_LABEL[st['release_state']] +
                                ('' if st['release_version'] is None else '(v%s)' % st['release_version']),
                     '交接': HANDOFF_LABEL.get(hand['state'], '-') if hand else '-',
                     '停交': STOP_LABEL.get(stop['state'], '-') if stop else '-'})
    return view, rows


def cmd_status(args):
    with Store(args.data_dir) as store:
        shipments = store.shipments()
        if args.shipment:
            shipments = [s for s in shipments if s[1] == args.shipment]
        if not shipments:
            print('(尚无运输批次记录,请先执行 collect)')
            return 0
        for carrier, shipment in shipments:
            view, rows = _box_rows(store, carrier, shipment)
            head = '批次 %s(承运 %s)' % (shipment, carrier)
            if view['missing_manifest']:
                head += ' [缺箱单,引用待核]'
            elif view['manifest_conflict']:
                head += ' [箱单版本冲突,待人工核实]'
            else:
                head += ' [箱单 v%s]' % view['manifest_version']
            print(head)
            _print(rows, ['批次', '箱号', '箱单内', '到货', '放行状态', '交接', '停交'])
    return 0


def cmd_missing(args):
    with Store(args.data_dir) as store:
        rows = []
        for carrier, shipment in store.shipments():
            view = shipment_view(store, carrier, shipment)
            if view['missing_manifest']:
                rows.append({'批次': shipment, '箱号': '-', '缺项': '箱单'})
                continue
            for box, st in view['boxes'].items():
                if not st['expected']:
                    continue
                lacks = []
                if st['release_state'] == 'none':
                    lacks.append('放行通知')
                if not st['arrived']:
                    lacks.append('到货回报')
                if st['blocked']:
                    lacks.append('冲突待核')
                if lacks:
                    rows.append({'批次': shipment, '箱号': box, '缺项': ','.join(lacks)})
        print('缺件及待核清单:')
        _print(rows, ['批次', '箱号', '缺项'])
    return 0


def cmd_detained(args):
    """已到货但被扣留的箱:交收货组现场处置的清单。"""
    with Store(args.data_dir) as store:
        rows = []
        for carrier, shipment in store.shipments():
            view = shipment_view(store, carrier, shipment)
            for box, st in view['boxes'].items():
                if st['expected'] and st['arrived'] and st['release_state'] in ('held', 'conflict'):
                    rows.append({'批次': shipment, '箱号': box,
                                 '状态': RELEASE_LABEL[st['release_state']] +
                                        ('' if st['release_version'] is None else '(v%s)' % st['release_version']),
                                 '处置': '收货组现场处置,不得入场'})
        print('已到货扣留箱(交收货组处置):')
        _print(rows, ['批次', '箱号', '状态', '处置'])
    return 0


def cmd_review(args):
    with Store(args.data_dir) as store:
        rows = [{'编号': r['id'], '类型': r['kind'], '说明': mask_text(r['summary']),
                 '状态': r['status']} for r in store.reviews(only_open=not args.all)]
        print('待人工核实:' if not args.all else '核实记录(含已了结):')
        _print(rows, ['编号', '类型', '说明', '状态'])
    return 0


def cmd_review_resolve(args):
    with Store(args.data_dir) as store:
        cycle = store.begin_cycle()
        try:
            from .resolve import resolve_review
            note = resolve_review(store, args.id, args.choose, cycle)
        except ValueError as exc:
            print('无法了结: ' + str(exc))
            return 1
        store.end_cycle(cycle, {'resolve': args.id})
        print('已了结: ' + mask_text(note))
    return 0


def cmd_stops(args):
    with Store(args.data_dir) as store:
        rows = [{'编号': s['reference'], '箱号': s['box'], '原因': mask_text(s['reason'] or ''),
                 '状态': STOP_LABEL.get(s['state'], s['state'])} for s in store.stops()]
        print('停交通知(未确认的需盯现场确认):')
        _print(rows, ['编号', '箱号', '原因', '状态'])
    return 0


def cmd_handoffs(args):
    with Store(args.data_dir) as store:
        rows = [{'编号': h['reference'], '箱号': h['box'], '回执': h['receipt'] or '-',
                 '状态': HANDOFF_LABEL.get(h['state'], h['state'])} for h in store.handoffs()]
        print('交接记录:')
        _print(rows, ['编号', '箱号', '回执', '状态'])
    return 0


def cmd_messages(args):
    with Store(args.data_dir) as store:
        rows = []
        for m in store.list_messages():
            folders = sorted({'%s#%s' % (l['folder'], l['uid']) for l in store.message_locations(m['hash'])})
            rows.append({'邮件': m['hash'][:12], 'Message-ID': mask_text(m['message_id']),
                         '发件人': mask_text(m['sender']), '主题': mask_text(m['subject']),
                         '位置': ','.join(folders)})
        print('已领取原件(身份为 MIME 字节哈希,Message-ID 可能重复):')
        _print(rows, ['邮件', 'Message-ID', '发件人', '主题', '位置'])
    return 0


def cmd_explain(args):
    with Store(args.data_dir) as store:
        msg = store.find_message(args.message)
        if not msg:
            print('未找到邮件(用 messages 命令查看哈希前缀或完整 Message-ID)')
            return 1
        print('邮件 %s' % msg['hash'][:12])
        print('  Message-ID: %s' % mask_text(msg['message_id']))
        print('  发件人: %s(仅读取范围参考,不作归属依据)' % mask_text(msg['sender']))
        print('  主题: %s(不作归属依据)' % mask_text(msg['subject']))
        for loc in store.message_locations(msg['hash']):
            print('  位置: %s UID %s(UIDVALIDITY %s,第 %s 轮见到)' %
                  (loc['folder'], loc['uid'], loc['uidvalidity'], loc['cycle']))
        for att in store.attachments_of(msg['hash']):
            print('  附件 %s [%s] %s' % (att['hash'][:12], att['status'],
                                        mask_text(att['filename'] or '(无名)')))
            if att['error']:
                print('    解析失败: %s' % mask_text(att['error']))
        related = [d for d in store.documents()
                   if any(s['msg_hash'] == msg['hash'] for s in
                          store.doc_sources(d['doc_key'], d['content_hash']))]
        for d in related:
            print('  归为单证: %s 承运=%s 批次=%s v%s [%s]' %
                  (d['type'], d['carrier'], d['shipment'], d['version'], d['status']))
            print('    覆盖箱号: %s' % ','.join(d['boxes']))
        reviews = [r for r in store.reviews(only_open=False) if r['msg_hash'] == msg['hash']]
        for r in reviews:
            print('  关联待核 #%s: %s(%s)' % (r['id'], mask_text(r['summary']), r['status']))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='collector', description='运输邮件采集与入场交接')
    parser.add_argument('--data-dir', default='var', help='采集程序自有状态目录(默认 var)')
    parser.add_argument('--imap-host', default='127.0.0.1')
    parser.add_argument('--imap-port', type=int, default=1143)
    parser.add_argument('--imap-user', default='collector')
    parser.add_argument('--imap-password', default='local-demo')
    parser.add_argument('--desk', default='http://127.0.0.1:8765', help='入场交接台地址')
    parser.add_argument('--read-senders', default='',
                        help='只读范围白名单(逗号分隔发件人);仅限定读取范围,不作归属依据')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('collect', help='从 IMAP 领取一轮并执行交接/停交')
    p.add_argument('--cycles', type=int, default=1, help='轮数(默认 1)')
    p.add_argument('--interval', type=float, default=5, help='多轮间隔秒数')
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser('status', help='各运输批次箱状态总览')
    p.add_argument('shipment', nargs='?', help='只看指定批次')
    p.set_defaults(func=cmd_status)

    sub.add_parser('missing', help='缺件及待核清单').set_defaults(func=cmd_missing)
    sub.add_parser('detained', help='已到货扣留箱(交收货组处置)').set_defaults(func=cmd_detained)

    p = sub.add_parser('review', help='待人工核实清单')
    p.add_argument('--all', action='store_true', help='含已了结')
    p.set_defaults(func=cmd_review)

    p = sub.add_parser('review-resolve', help='人工核实后了结(同版冲突需选定内容)')
    p.add_argument('id', type=int, help='待核编号')
    p.add_argument('--choose', help='选定内容哈希前缀(同版冲突必选)')
    p.set_defaults(func=cmd_review_resolve)

    sub.add_parser('stops', help='停交通知及现场确认状态').set_defaults(func=cmd_stops)
    sub.add_parser('handoffs', help='交接记录与回执').set_defaults(func=cmd_handoffs)
    sub.add_parser('messages', help='已领取原件清单(脱敏)').set_defaults(func=cmd_messages)

    p = sub.add_parser('explain', help='解释一封邮件被归到哪里')
    p.add_argument('message', help='邮件哈希前缀或完整 Message-ID')
    p.set_defaults(func=cmd_explain)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
