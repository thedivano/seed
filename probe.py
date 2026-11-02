import json, alert, traceback
out={}
try:
    day,live,m1=alert.us_load("NVDA")
    wk,m1b,m10,m30=alert.us_extras("NVDA",m1)
    out={"day_last":day[-3:],"n_day":len(day),"live":live,"krx":alert.KRX["NVDA"],"info":alert.INFO["NVDA"],"n":[len(wk),len(m1),len(m10),len(m30)],"m10_last":m10[-2:],"wk_last":wk[-1]}
except Exception: out={"err":traceback.format_exc()}
open("probe.json","w").write(json.dumps(out,ensure_ascii=False,indent=1,default=str))
