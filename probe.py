import requests, json
UA={"User-Agent":"Mozilla/5.0"}
out={}
for tf,cnt in [("minute",5),("week",3),("day",3)]:
    r=requests.get("https://fchart.stock.naver.com/sise.nhn",params={"symbol":"005930","timeframe":tf,"count":cnt,"requestType":0},headers=UA,timeout=15)
    out["fchart_"+tf]=r.text[-600:]
r=requests.get("https://api.finance.naver.com/siseJson.naver",params={"symbol":"005930","requestType":1,"startTime":"202610290800","endTime":"202610291000","timeframe":"minute"},headers=UA,timeout=15)
out["siseJson_minute"]=r.text[:600]
r=requests.get("https://fchart.stock.naver.com/sise.nhn",params={"symbol":"005930","timeframe":"minute","count":4000,"requestType":0},headers=UA,timeout=15)
items=[l for l in r.text.splitlines() if 'item data' in l]
out["minute_count"]=len(items); out["minute_first"]=items[:3]; out["minute_last"]=items[-3:]
open("probe.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
