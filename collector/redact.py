"""查询输出脱敏:合成联系人信息(邮箱地址、长数字串)不进入普通查询结果。

原始 MIME 与附件完整保存在受限目录,脱敏只发生在展示层。
"""
import re

EMAIL = re.compile(r'([A-Za-z0-9._%+-])([A-Za-z0-9._%+-]*)(@[A-Za-z0-9.-]+)')
PHONE = re.compile(r'(?<!\d)(\d{3})\d{4,}(\d{2})(?!\d)')


def mask_text(value):
    """把文本中的邮箱本地部分和长数字串打码,其余内容原样保留。"""
    if value is None:
        return ''
    text = str(value)
    text = EMAIL.sub(lambda m: m.group(1) + '***' + m.group(3), text)
    text = PHONE.sub(lambda m: m.group(1) + '****' + m.group(2), text)
    return text


def mask_mapping(row):
    """对 dict 的所有字符串值做脱敏,用于 CLI 行输出。"""
    return {k: mask_text(v) if isinstance(v, str) else v for k, v in row.items()}
