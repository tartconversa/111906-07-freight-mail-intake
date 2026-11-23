"""入场交接台 HTTP 客户端。

交接台只按 reference 收件并返回箱级回执;采集程序每轮先 GET 已有交接
与停交记录再决定是否提交,断线/丢响应后靠查询对齐,不重复安排同一箱。
"""
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class DeskError(RuntimeError):
    """交接台不可达或响应异常(含先保存后丢响应的 503)。"""


class Desk:
    def __init__(self, base, timeout=10):
        self.base = base.rstrip('/')
        self.timeout = timeout

    def _request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        req = Request(self.base + path, data=data, headers=headers)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except HTTPError as exc:
            # 交接台可能先保存再返回 503(lose_response):按失败处理,
            # 下一轮 GET 查询会揭示真实结果,绝不盲目重发。
            raise DeskError('交接台返回 %s: %s' % (exc.code, exc.read()[:200])) from exc
        except (URLError, OSError) as exc:
            raise DeskError('交接台不可达: %s' % exc) from exc

    def list_handoffs(self):
        return self._request('/handoff')

    def list_stops(self):
        return self._request('/stop')

    def post_handoff(self, reference, carrier, shipment, boxes, evidence):
        return self._request('/handoff', {
            'reference': reference, 'carrier': carrier, 'shipment': shipment,
            'boxes': boxes, 'evidence': evidence})

    def post_stop(self, reference, handoff_reference, boxes, reason):
        return self._request('/stop', {
            'reference': reference, 'handoff_reference': handoff_reference,
            'boxes': boxes, 'reason': reason})
