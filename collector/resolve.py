"""人工核实后的了结动作:同版冲突必须明确选定一份内容,不按先后取舍。"""

from .reconcile import shipment_view


def _conflict_rows(store, review):
    """返回该待核项冲突涉及的单证行集合。"""
    if review['kind'] == 'same_version_conflict' and review['doc_key']:
        return store.get_document_rows(review['doc_key'])
    if review['kind'] == 'release_hold_conflict':
        parts = review['rkey'].split('|')
        if len(parts) != 4:
            return []
        _, carrier, shipment, box = parts
        view = shipment_view(store, carrier, shipment)
        st = view['boxes'].get(box)
        if not st or st['release_state'] != 'conflict':
            return []
        top = st['release_version']
        return [d for d in store.documents(carrier, shipment)
                if d['type'] in ('release', 'hold') and d['version'] == top
                and box in d['boxes']]
    return []


def resolve_review(store, review_id, choose, cycle):
    """了结待核项。冲突类必须用 choose 选定内容哈希前缀。返回说明文字。"""
    review = next((r for r in store.reviews(only_open=False) if r['id'] == review_id), None)
    if review is None:
        raise ValueError('无此待核编号')
    if review['status'] != 'open':
        raise ValueError('该待核项已了结')
    rows = _conflict_rows(store, review)
    if rows:
        if not choose:
            raise ValueError('同版冲突必须用 --choose 选定一份内容(哈希前缀)')
        hits = [r for r in rows if r['content_hash'].startswith(choose)]
        if len(hits) != 1:
            raise ValueError('哈希前缀未唯一匹配冲突内容')
        chosen = hits[0]
        for r in rows:
            store.set_document_status(r['doc_key'], r['content_hash'],
                                      'applied' if r is chosen else 'superseded')
        note = '人工选定 %s v%s 内容 %s' % (chosen['type'], chosen['version'],
                                            chosen['content_hash'][:12])
    else:
        if choose:
            raise ValueError('该待核项不涉及内容冲突,不需要 --choose')
        note = '人工核实了结'
    store.resolve_review(review_id, note, cycle)
    return '%s(#%d %s)' % (note, review_id, review['kind'])
