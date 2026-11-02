import requests, json
UA={"User-Agent":"Mozilla/5.0"}
out={}
tests={
 "yahoo_chart":"https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d",
 "yahoo_1m":"https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=5m&includePrePost=true",
 "naver_world_basic":"https://api.stock.naver.com/stock/AAPL.O/basic",
 "naver_world_chart":"https://api.stock.naver.com/chart/foreign/item/AAPL.O/day?startDateTime=202610200000&endDateTime=202611030000",
 "naver_search":"https://ac.stock.naver.com/ac?q=apple&target=stock,index",
 "yahoo_search":"https://query1.finance.yahoo.com/v1/finance/search?q=nvidia&quotesCount=5",
}
for k,u in tests.items():
    try:
        r=requests.get(u,headers=UA,timeout=15); out[k]={"status":r.status_code,"body":r.text[:1500]}
    except Exception as e: out[k]=str(e)
open("probe.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
