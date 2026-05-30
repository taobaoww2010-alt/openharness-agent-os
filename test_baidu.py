import asyncio
import httpx

async def fetch_baidu():
    ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    headers = {'User-Agent': ua, 'Accept': 'text/html', 'Accept-Language': 'zh-CN,zh;q=0.9'}
    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as c:
        r = await c.get('https://www.baidu.com/s', params={'wd': '北京天气'}, headers=headers)
        body = r.text
        
        # Check for different patterns
        print('result-op:', 'result-op' in body)
        print('content_left:', 'id="content_left"' in body)
        print('class="t":', 'class="t"' in body)
        print('h3:', 'h3' in body)
        
        # Find the results section
        start = body.find('id="content_left"')
        if start != -1:
            end = body.find('id="content_right"', start)
            if end == -1:
                end = start + 5000
            print('\nResults section preview:')
            print(body[start:min(end, start+3000)])

asyncio.run(fetch_baidu())