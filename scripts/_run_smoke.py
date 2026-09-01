import traceback

try:
    exec(open("scripts/smoke_test.py", encoding="utf-8").read())
except Exception:
    with open("smoke_traceback.txt", "w", encoding="utf-8") as f:
        traceback.print_exc(file=f)
    print("SMOKE FAILED — see smoke_traceback.txt")
