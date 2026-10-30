import requests, json
UA={"User-Agent":"Mozilla/5.0"}
out={}
for c in ["005930","000660","000150"]:
    d=requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{c}",headers=UA,timeout=15).json()["datas"][0]
    o=d.get("overMarketPriceInfo") or {}; ip=d.get("integratedPriceInfo") or {}
    out[c]={"closePrice":d.get("closePrice"),"cmp":d.get("compareToPreviousClosePrice"),"ratio":d.get("fluctuationsRatio"),"status":d.get("marketStatus"),
            "over":[o.get("tradingSessionType"),o.get("overMarketStatus"),o.get("overPrice"),o.get("compareToPreviousClosePrice"),o.get("fluctuationsRatio")],
            "integrated":{k:ip.get(k) for k in list(ip)[:12]}}
    out[c]["sise"]=requests.get("https://api.finance.naver.com/siseJson.naver",params={"symbol":c,"requestType":1,"startTime":"20261028","endTime":"20261030","timeframe":"day"},headers=UA,timeout=15).text[-260:]
open("probe.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
