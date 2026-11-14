"""当前单响应概览。"""
def analyze(response):
    text=response.body.decode('utf-8')
    lines=text.replace('\r\n ','').splitlines()
    return {'event_components':sum(line=='BEGIN:VEVENT' for line in lines),
            'uids':[line[4:] for line in lines if line.startswith('UID:')]}
