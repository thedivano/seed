import requests, json
UA={"User-Agent":"Mozilla/5.0"}
out={}
for k,u in {"polling":"https://polling.finance.naver.com/api/realtime/domestic/stock/005930",
            "integration":"https://m.stock.naver.com/api/stock/005930/integration",
            "price":"https://m.stock.naver.com/api/stock/005930/price?pageSize=3&page=1"}.items():
    try: out[k]=requests.get(u,headers=UA,timeout=15).text[:3500]
    except Exception as e: out[k]=str(e)
open("probe.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
