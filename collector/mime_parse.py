"""MIME 原件解析:只从附件内容认定单证归属,主题/发件人/文件名不作依据。

每封邮件应带 application/json 附件(单证)。单证字段:
type(manifest/release/hold/arrival)、carrier、shipment、version、boxes。
解析失败或字段缺失的原件交给人工核实,不从主题猜结论。
"""
import json
from email.parser import BytesParser
from email.policy import default

DOC_TYPES = ('manifest', 'release', 'hold', 'arrival')
REQUIRED = ('type', 'carrier', 'shipment', 'version', 'boxes')


class DocumentError(ValueError):
    """单证无法解析或字段不满足约束。"""


def validate_document(payload):
    """校验并规范化一份单证 dict,不合格时抛 DocumentError。"""
    if not isinstance(payload, dict):
        raise DocumentError('单证不是 JSON 对象')
    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        raise DocumentError('缺少字段: ' + ','.join(missing))
    doc_type = payload['type']
    if doc_type not in DOC_TYPES:
        raise DocumentError('未知单证类型: %r' % (doc_type,))
    carrier, shipment = payload['carrier'], payload['shipment']
    if not isinstance(carrier, str) or not carrier.strip():
        raise DocumentError('carrier 为空')
    if not isinstance(shipment, str) or not shipment.strip():
        raise DocumentError('shipment 为空')
    version = payload['version']
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DocumentError('version 必须是正整数')
    boxes = payload['boxes']
    if not isinstance(boxes, list) or not boxes or any(not isinstance(b, str) or not b.strip() for b in boxes):
        raise DocumentError('boxes 必须是非空字符串数组')
    if any('|' in v for v in [carrier, shipment] + list(boxes)):
        raise DocumentError('carrier/shipment/boxes 含非法字符 |')
    return {
        'type': doc_type,
        'carrier': carrier.strip(),
        'shipment': shipment.strip(),
        'version': version,
        'boxes': sorted({b.strip() for b in boxes}),
    }


def doc_key(doc):
    """单证业务键:同承运/批次/类型/版本应为同一份单证。"""
    return '%s|%s|%s|%d' % (doc['type'], doc['carrier'], doc['shipment'], doc['version'])


def parse_message(raw):
    """解析 MIME 字节,返回 {'headers': {...}, 'attachments': [...]}。

    attachments 每项: {'filename', 'content_type', 'payload'(bytes)}。
    只取 application/json 附件作为候选单证,其余附件保留记录但不解释。
    """
    msg = BytesParser(policy=default).parsebytes(raw)
    headers = {
        'message_id': str(msg.get('Message-ID', '')).strip(),
        'sender': str(msg.get('From', '')).strip(),
        'subject': str(msg.get('Subject', '')).strip(),
    }
    attachments = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b''
        attachments.append({
            'filename': part.get_filename() or '',
            'content_type': part.get_content_type(),
            'payload': payload,
        })
    return {'headers': headers, 'attachments': attachments}


def extract_documents(attachments):
    """从附件列表提取 (doc, attachment) 或 (error, attachment) 对。

    返回 (documents, failures):documents 为 (doc, attachment) 列表,
    failures 为 (错误信息, attachment) 列表。没有 JSON 附件时两者皆空。
    """
    documents, failures = [], []
    for att in attachments:
        if att['content_type'] != 'application/json':
            continue
        try:
            payload = json.loads(att['payload'].decode('utf-8'))
            documents.append((validate_document(payload), att))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(('JSON 无法解析: %s' % exc, att))
        except DocumentError as exc:
            failures.append((str(exc), att))
    return documents, failures
