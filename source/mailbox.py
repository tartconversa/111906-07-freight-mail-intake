"""可供标准imaplib连接的隔离合成邮箱。"""
import socketserver,threading,re
from email.message import EmailMessage
from email.policy import SMTP
def mime(item):
    from .common import dump
    msg=EmailMessage(policy=SMTP);msg['From']=item['sender'];msg['To']='freight@example.test';msg['Subject']=item['subject'];msg['Message-ID']=item['message_id'];msg.set_content('合成运输业务邮件，请核对原件内容。')
    msg.add_attachment(dump(item['document']).encode(),maintype='application',subtype='json',filename='documents.json')
    # 固定边界保证同一封邮件在移动前后原始字节相同。
    msg.set_boundary('local-freight-'+item['key'])
    return msg.as_bytes()
def create_mailbox(app,port):
    class Handler(socketserver.StreamRequestHandler):
        def send(self,data): self.wfile.write(data);self.wfile.flush()
        def handle(self):
            self.send(b'* OK local freight IMAP4rev1 ready\r\n');folder='INBOX';logged=False
            while True:
                raw=self.rfile.readline()
                if not raw: break
                parts=raw.decode().strip().split(' ',2)
                if len(parts)<2: break
                tag,cmd=parts[:2];args=parts[2] if len(parts)>2 else '';cmd=cmd.upper();prefix=tag.encode()
                if cmd=='CAPABILITY': self.send(b'* CAPABILITY IMAP4rev1 UIDPLUS\r\n'+prefix+b' OK capability\r\n');continue
                if cmd=='LOGIN':
                    logged='collector' in args and 'local-demo' in args
                    self.send(prefix+(b' OK login\r\n' if logged else b' NO credentials\r\n'));continue
                if cmd=='LOGOUT': self.send(b'* BYE logout\r\n'+prefix+b' OK logout\r\n');break
                if not logged: self.send(prefix+b' NO login required\r\n');continue
                with app.lock:
                    state=app.current
                    if cmd=='LIST':
                        for name in state['folders']: self.send(('* LIST () "/" "'+name+'"\r\n').encode())
                    elif cmd in ['SELECT','EXAMINE']:
                        folder=args.strip('"')
                        if folder not in state['folders']: self.send(prefix+b' NO missing folder\r\n');continue
                        count=len(state['folders'][folder]);self.send(f'* {count} EXISTS\r\n* OK [UIDVALIDITY {state["uidvalidity"]}] generation\r\n'.encode())
                    elif cmd=='UID':
                        sub,_,tail=args.partition(' ');mappings=state['folders'].get(folder,{})
                        if sub.upper()=='SEARCH':
                            threshold=0
                            match=re.search(r'UID\s+(\d+):\*',tail,re.I)
                            if match: threshold=int(match.group(1))
                            uids=[uid for uid in mappings if int(uid)>=threshold];self.send(('* SEARCH '+' '.join(uids)+'\r\n').encode())
                        elif sub.upper()=='FETCH':
                            requested=tail.split(' ')[0]
                            for index,(uid,key) in enumerate(mappings.items(),1):
                                if requested not in ['1:*','*',uid] and uid not in requested.split(','): continue
                                item=next(m for m in app.seed['messages'] if m['key']==key);data=mime(item)
                                self.send(f'* {index} FETCH (UID {uid} BODY[] {{{len(data)}}}\r\n'.encode()+data+b')\r\n')
                        else: self.send(prefix+b' BAD unsupported uid command\r\n');continue
                    elif cmd not in ['NOOP','CLOSE']: self.send(prefix+b' BAD unsupported command\r\n');continue
                self.send(prefix+b' OK completed\r\n')
    server=socketserver.ThreadingTCPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    return server
