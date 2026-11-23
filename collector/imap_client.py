"""真实 IMAP 只读领取:只用 EXAMINE + UID SEARCH + UID FETCH(BODY.PEEK[])。

UID 只在当前 UIDVALIDITY 内有效:世代变化后本地 UID 记录作废,整文件夹
重扫;邮件身份用 MIME 字节哈希,移动到归档文件夹后仍是同一份原件。
"""
import hashlib
import imaplib


class ImapError(RuntimeError):
    pass


def _check(status, data):
    if status != 'OK':
        raise ImapError('IMAP 命令失败: %r %r' % (status, data))


class ImapCollector:
    def __init__(self, host, port, user, password):
        self.host, self.port, self.user, self.password = host, port, user, password

    def fetch_new(self, store, cycle):
        """扫描全部文件夹,把新 UID 的原件落库,返回 [(msg_hash, raw)]。"""
        mail = imaplib.IMAP4(self.host, self.port)
        try:
            _check(*mail.login(self.user, self.password))
            fetched = []
            for folder in self._folders(mail):
                fetched.extend(self._scan(mail, store, cycle, folder))
            return fetched
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _folders(self, mail):
        status, lines = mail.list()
        _check(status, lines)
        names = []
        for line in lines or []:
            if isinstance(line, bytes):
                names.append(line.decode().rsplit(' ', 1)[-1].strip('"'))
        return names

    def _scan(self, mail, store, cycle, folder):
        _check(*mail.select(folder, readonly=True))
        uidvalidity = int(mail.response('UIDVALIDITY')[1][0])
        known = store.get_folder(folder)
        last_uid = known['last_uid'] if known and known['uidvalidity'] == uidvalidity else 0
        # UIDVALIDITY 变化(邮箱重建)后旧 UID 全部作废,从 1 重扫。
        status, data = mail.uid('search', None, 'UID', '%d:*' % (last_uid + 1))
        _check(status, data)
        uids = data[0].split() if data and data[0] else []
        fetched = []
        for uid in uids:
            uid_s = uid.decode()
            status, parts = mail.uid('fetch', uid_s, '(BODY.PEEK[])')
            _check(status, parts)
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw is None:
                continue
            msg_hash = hashlib.sha256(raw).hexdigest()
            if not store.has_message(msg_hash):
                fetched.append((msg_hash, raw))
            store.add_location(msg_hash, folder, int(uid_s), uidvalidity, cycle)
            last_uid = max(last_uid, int(uid_s))
        store.set_folder(folder, uidvalidity, last_uid)
        return fetched
