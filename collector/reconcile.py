"""单证落账与箱状态裁决。

规则(与 docs/source-contract.md 一致,且只以原件内容为准):
- manifest 定义批次应有箱范围,取最高版本;同版不同内容为冲突,待人工核实。
- 同一 carrier/shipment/box 的 release 与 hold 按 version 决出当前状态;
  同版相反结论(release 与 hold 同版本)为冲突,待人工核实。
- 缺明确 release 的箱不可交;arrival 独立决定实际到货。
- 旧邮件晚到不影响裁决:版本低的单证永远输给版本高的,与收信顺序无关。
"""
import hashlib
import json

from .mime_parse import doc_key


def content_hash(doc):
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def handoff_reference(carrier, shipment, box):
    return 'handoff:%s:%s:%s' % (carrier, shipment, box)


def stop_reference(carrier, shipment, box):
    return 'stop:%s:%s:%s' % (carrier, shipment, box)


def apply_document(store, doc, msg_hash, att_hash, cycle):
    """落账一份单证,返回 'applied' / 'duplicate' / 'conflict'。

    同一 doc_key(类型+承运+批次+版本)内容必须一致;内容不同即同版冲突,
    新旧内容都标记 conflicted 等待人工核实,不按到达先后取舍。
    """
    key, chash = doc_key(doc), content_hash(doc)
    store.add_doc_source(key, chash, msg_hash, att_hash)
    rows = store.get_document_rows(key)
    if any(r['content_hash'] == chash for r in rows):
        return 'duplicate'
    if rows:
        store.add_document(doc, chash, 'conflicted', msg_hash, att_hash, cycle)
        for r in rows:
            if r['status'] == 'applied':
                store.set_document_status(key, r['content_hash'], 'conflicted')
        hashes = sorted([r['content_hash'][:12] for r in rows] + [chash[:12]])
        store.open_review(
            'doc|' + key, 'same_version_conflict',
            '单证 %s 出现同版不同内容(%d 份)' % (key, len(rows) + 1),
            '内容哈希: ' + ', '.join(hashes), msg_hash, key, cycle)
        return 'conflict'
    store.add_document(doc, chash, 'applied', msg_hash, att_hash, cycle)
    return 'applied'


def _deciding(rows):
    """从一组行中取裁决行:最高版本;该版本有 conflicted 行则整体冲突。"""
    if not rows:
        return None, False
    top = max(r['version'] for r in rows)
    at_top = [r for r in rows if r['version'] == top]
    if any(r['status'] == 'conflicted' for r in at_top):
        return None, True
    applied = [r for r in at_top if r['status'] == 'applied']
    return (applied[0] if applied else None), False


def shipment_view(store, carrier, shipment):
    """计算一个运输批次的当前箱状态。"""
    docs = store.documents(carrier, shipment)
    manifests = [d for d in docs if d['type'] == 'manifest']
    view = {'carrier': carrier, 'shipment': shipment, 'missing_manifest': not manifests,
            'manifest_conflict': False, 'manifest_version': None, 'boxes': {}, 'out_of_scope': {}}
    scope = set()
    if manifests:
        deciding, conflict = _deciding(manifests)
        view['manifest_conflict'] = conflict
        if deciding:
            view['manifest_version'] = deciding['version']
            scope = set(deciding['boxes'])
    covered = {}
    for d in docs:
        if d['status'] != 'applied':
            continue
        for box in d['boxes']:
            covered.setdefault(box, []).append(d)
    rel_hold_all = {}
    for d in docs:
        if d['type'] in ('release', 'hold'):
            for box in d['boxes']:
                rel_hold_all.setdefault(box, []).append(d)
    for box in sorted(scope | set(covered)):
        st = {'box': box, 'expected': box in scope, 'arrived': False,
              'release_state': 'none', 'release_version': None, 'blocked': False}
        # 放行/扣留裁决考虑全部版本行(含 conflicted),冲突不会被当成"缺放行"。
        rel_hold = rel_hold_all.get(box, [])
        top = max((d['version'] for d in rel_hold), default=None)
        if top is not None:
            at_top = [d for d in rel_hold if d['version'] == top]
            types = {d['type'] for d in at_top if d['status'] == 'applied'}
            st['release_version'] = top
            if any(d['status'] == 'conflicted' for d in at_top) or len(types) > 1:
                # 同版不同内容,或同版相反结论(release 与 hold 同版本):待人工核实。
                st['release_state'], st['blocked'] = 'conflict', True
            elif types:
                st['release_state'] = 'released' if types == {'release'} else 'held'
        st['arrived'] = any(d['type'] == 'arrival' for d in covered.get(box, []))
        if view['manifest_conflict']:
            st['blocked'] = True
        view['boxes'][box] = st
        if covered.get(box) and box not in scope:
            view['out_of_scope'][box] = sorted({d['doc_key'] for d in covered[box]})
    return view


def evidence_for(store, carrier, shipment, box):
    """交接证据:支撑该箱放行的原件摘要(单证哈希+邮件哈希),不含联系人。"""
    docs = store.documents(carrier, shipment)
    evidence = []
    for doc_type in ('manifest', 'release', 'arrival'):
        rows = [d for d in docs if d['type'] == doc_type and d['status'] == 'applied'
                and box in d['boxes']]
        if not rows:
            continue
        row = max(rows, key=lambda r: r['version'])
        evidence.append({'type': doc_type, 'version': row['version'],
                         'document': row['content_hash'][:16], 'message': row['first_msg'][:16]})
    return evidence


def refresh_reviews(store, cycle):
    """按当前状态开合待核项:引用不明、箱单外箱号、同版相反结论。"""
    for carrier, shipment in store.shipments():
        view = shipment_view(store, carrier, shipment)
        rkey = 'nomani|%s|%s' % (carrier, shipment)
        if view['missing_manifest']:
            store.open_review(rkey, 'unknown_shipment',
                              '批次 %s/%s 有单证但无箱单,引用不明' % (carrier, shipment),
                              '', None, None, cycle)
        else:
            _auto_resolve(store, rkey, '箱单已到', cycle)
        for box, keys in view['out_of_scope'].items():
            store.open_review('scope|%s|%s|%s' % (carrier, shipment, box), 'box_out_of_scope',
                              '箱 %s 不在批次 %s 箱单范围内,却被单证引用' % (box, shipment),
                              '引用单证: ' + ', '.join(keys), None, None, cycle)
        open_scope = {r['rkey'] for r in store.reviews() if r['kind'] == 'box_out_of_scope'}
        for rkey in open_scope:
            parts = rkey.split('|')
            if len(parts) == 4 and parts[1] == carrier and parts[2] == shipment \
                    and parts[3] not in view['out_of_scope']:
                _auto_resolve(store, rkey, '箱号已进入箱单范围或引用失效', cycle)
        for box, st in view['boxes'].items():
            rkey = 'rh|%s|%s|%s' % (carrier, shipment, box)
            if st['release_state'] == 'conflict':
                store.open_review(rkey, 'release_hold_conflict',
                                  '箱 %s 放行/扣留同版(v%s)相反结论' % (box, st['release_version']),
                                  '', None, None, cycle)
            else:
                _auto_resolve(store, rkey, '已有更新版本裁决', cycle)
    # 同版内容冲突在出现更高版本后不再决定状态,自动了结。
    for review in store.reviews():
        if review['kind'] != 'same_version_conflict' or not review['doc_key']:
            continue
        parts = review['doc_key'].split('|')
        doc_type, carrier, shipment, version = parts[0], parts[1], parts[2], int(parts[3])
        newer = [d for d in store.documents(carrier, shipment)
                 if d['type'] == doc_type and d['version'] > version and d['status'] == 'applied']
        if newer:
            _auto_resolve(store, review['rkey'], '已被 v%d 取代' % max(d['version'] for d in newer), cycle)


def _auto_resolve(store, rkey, note, cycle):
    for review in store.reviews():
        if review['rkey'] == rkey:
            store.resolve_review(review['id'], note, cycle, auto=True)


def plan_actions(store):
    """根据当前状态生成交接/停交计划(不接触交接台)。"""
    plan = {'handoffs': [], 'stops': []}
    for carrier, shipment in store.shipments():
        view = shipment_view(store, carrier, shipment)
        if view['missing_manifest'] or view['manifest_conflict']:
            continue
        for box, st in view['boxes'].items():
            if not st['expected']:
                continue
            ref = handoff_reference(carrier, shipment, box)
            sref = stop_reference(carrier, shipment, box)
            hand = store.get_handoff(ref, box)
            stop = store.get_stop(sref)
            stopped = stop and stop['state'] in ('pending', 'confirmed', 'unknown')
            eligible = st['arrived'] and st['release_state'] == 'released' and not st['blocked']
            if eligible:
                # 已停交的箱不自动重交,需收货组处置后人工重置。
                if not stopped and (hand is None or hand['state'] in ('lost', 'unknown')):
                    plan['handoffs'].append({
                        'reference': ref, 'carrier': carrier, 'shipment': shipment, 'box': box,
                        'evidence': evidence_for(store, carrier, shipment, box)})
            elif hand and hand['state'] == 'accepted' and not stopped:
                reason = {'held': '扣留通知 v%s 生效' % st['release_version'],
                          'conflict': '放行/扣留同版冲突,待人工核实',
                          'none': '放行依据缺失'}.get(st['release_state'], '单证状态变化')
                plan['stops'].append({'reference': sref, 'handoff_reference': ref,
                                      'carrier': carrier, 'shipment': shipment,
                                      'box': box, 'reason': reason})
    return plan
