"""一轮采集:IMAP 领取 → 解析落账 → 待核开合 → 交接台对齐 → 执行交接/停交。

交接台交互顺序固定为"先查后交":每轮先 GET 已有交接与停交,对齐本地
状态(断线、丢响应、来源重启都靠这一步发现),再只提交缺口。
"""
import hashlib
from dataclasses import dataclass, field

from .desk import Desk, DeskError
from .imap_client import ImapCollector, ImapError
from .mime_parse import extract_documents, parse_message
from .reconcile import apply_document, plan_actions, refresh_reviews


@dataclass
class Config:
    imap_host: str = '127.0.0.1'
    imap_port: int = 1143
    imap_user: str = 'collector'
    imap_password: str = 'local-demo'
    desk: str = 'http://127.0.0.1:8765'
    data_dir: str = 'var'
    read_senders: tuple = ()  # 只读范围白名单;空为全部读取。仅限定读取范围,不作归属依据。


def process_message(store, msg_hash, raw, cycle, config, summary):
    """解析一封原件并落账。返回 True 表示进入了解析(未被白名单跳过)。"""
    parsed = parse_message(raw)
    headers = parsed['headers']
    raw_path = store.save_raw(msg_hash, raw, 'msg')
    store.add_message(msg_hash, headers, raw_path, cycle)
    sender = headers['sender']
    if config.read_senders and sender not in config.read_senders:
        summary['skipped'].append(msg_hash[:12])
        return False
    documents, failures = extract_documents(parsed['attachments'])
    if not documents and not failures:
        store.open_review('nodoc|' + msg_hash, 'no_document',
                          '邮件 %s 无 JSON 单证附件' % msg_hash[:12], '', msg_hash, None, cycle)
    for att in parsed['attachments']:
        att_hash = hashlib.sha256(att['payload']).hexdigest()
        path = store.save_raw(att_hash, att['payload'], 'att')
        if att['content_type'] != 'application/json':
            store.add_attachment(att_hash, msg_hash, att['filename'], att['content_type'], path, 'ignored')
            continue
        failure = next((msg for msg, a in failures if a is att), None)
        if failure:
            store.add_attachment(att_hash, msg_hash, att['filename'], att['content_type'], path, 'failed', failure)
            store.open_review('bad|' + att_hash, 'unparseable',
                              '附件 %s 无法解析: %s' % (att['filename'] or att_hash[:12], failure),
                              '', msg_hash, None, cycle)
            continue
        store.add_attachment(att_hash, msg_hash, att['filename'], att['content_type'], path, 'parsed')
    for doc, att in documents:
        att_hash = hashlib.sha256(att['payload']).hexdigest()
        result = apply_document(store, doc, msg_hash, att_hash, cycle)
        summary['documents'].setdefault(result, []).append(
            '%s/%s v%d' % (doc['shipment'], doc['type'], doc['version']))
    return True


def sync_desk(store, desk, cycle, summary):
    """与交接台对齐:采纳远端已有记录,标记本地失效记录。远端不可达时跳过。"""
    try:
        remote_handoffs = desk.list_handoffs()
        remote_stops = desk.list_stops()
    except DeskError as exc:
        summary['errors'].append(str(exc))
        return None, None
    for ref, item in remote_handoffs.items():
        payload = item.get('payload', {})
        for box in payload.get('boxes', []):
            store.upsert_handoff(ref, box, payload.get('carrier', ''), payload.get('shipment', ''),
                                 item.get('receipt'), 'accepted', cycle)
    for row in store.handoffs():
        if row['state'] == 'accepted' and row['reference'] not in remote_handoffs:
            store.upsert_handoff(row['reference'], row['box'], row['carrier'], row['shipment'],
                                 row['receipt'], 'lost', cycle)
    for ref, item in remote_stops.items():
        payload = item.get('payload', {})
        boxes = payload.get('boxes') or ['']
        store.upsert_stop(ref, payload.get('handoff_reference', ''), payload.get('carrier', ''),
                          payload.get('shipment', ''), boxes[0], payload.get('reason', ''),
                          item.get('state', 'pending'), cycle)
    for row in store.stops():
        if row['state'] == 'confirmed' and row['reference'] not in remote_stops:
            store.upsert_stop(row['reference'], row['handoff_reference'], row['carrier'],
                              row['shipment'], row['box'], row['reason'], 'lost', cycle)
    return remote_handoffs, remote_stops


def run_cycle(config, store):
    """执行一轮采集,返回摘要 dict(全部键均为可脱敏展示的安全内容)。"""
    cycle = store.begin_cycle()
    summary = {'cycle': cycle, 'new_messages': [], 'skipped': [], 'documents': {},
               'handoffs': [], 'stops': [], 'errors': []}
    try:
        fetched = ImapCollector(config.imap_host, config.imap_port,
                                config.imap_user, config.imap_password).fetch_new(store, cycle)
    except ImapError as exc:
        summary['errors'].append(str(exc))
        store.end_cycle(cycle, summary)
        return summary
    for msg_hash, raw in fetched:
        if process_message(store, msg_hash, raw, cycle, config, summary):
            summary['new_messages'].append(msg_hash[:12])
    refresh_reviews(store, cycle)
    desk = Desk(config.desk)
    remote_handoffs, _ = sync_desk(store, desk, cycle, summary)
    if remote_handoffs is not None:
        plan = plan_actions(store)
        for item in plan['handoffs']:
            if item['reference'] in remote_handoffs:
                continue  # 远端已有同 reference 交接,断线重连后不重复安排
            try:
                desk.post_handoff(item['reference'], item['carrier'], item['shipment'],
                                  [item['box']], item['evidence'])
                receipt = desk.list_handoffs()[item['reference']].get('receipt')
                store.upsert_handoff(item['reference'], item['box'], item['carrier'],
                                     item['shipment'], receipt, 'accepted', cycle)
                summary['handoffs'].append(item['reference'])
            except DeskError as exc:
                # 可能已保存但响应丢失:记 unknown,下轮先查再定。
                store.upsert_handoff(item['reference'], item['box'], item['carrier'],
                                     item['shipment'], None, 'unknown', cycle)
                summary['errors'].append(str(exc))
        for item in plan['stops']:
            try:
                desk.post_stop(item['reference'], item['handoff_reference'],
                               [item['box']], item['reason'])
                state = desk.list_stops()[item['reference']].get('state', 'pending')
                store.upsert_stop(item['reference'], item['handoff_reference'], item['carrier'],
                                  item['shipment'], item['box'], item['reason'], state, cycle)
                summary['stops'].append(item['reference'])
            except DeskError as exc:
                store.upsert_stop(item['reference'], item['handoff_reference'], item['carrier'],
                                  item['shipment'], item['box'], item['reason'], 'unknown', cycle)
                summary['errors'].append(str(exc))
    store.end_cycle(cycle, summary)
    return summary
