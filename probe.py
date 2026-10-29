import requests, json
UA={"User-Agent":"Mozilla/5.0"}
urls={
 "polling":"https://polling.finance.naver.com/api/realtime/domestic/stock/005930",
 "m_basic":"https://m.stock.naver.com/api/stock/005930/basic",
 "m_integration":"https://m.stock.naver.com/api/stock/005930/integration",
 "fchart_min":"https://fchart.stock.naver.com/sise.nhn?symbol=005930&timeframe=minute&count=3&requestType=0",
}
out={}
for k,u in urls.items():
    try:
        r=requests.get(u,headers=UA,timeout=15); out[k]={"status":r.status_code,"body":r.text[:2500]}
    except Exception as e: out[k]=str(e)
open("probe.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
