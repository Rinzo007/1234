import json, os

path = os.path.join(os.environ["TEMP"], "rw_health.json")
d = json.load(open(path, encoding="utf-8"))
targets = {
    "od/io.py",
    "overture/http.py",
    "overture/load.py",
    "config/build.py",
    "passenger_flow/network/routes.py",
}
for f in d["findings"]:
    if f["file_path"] not in targets:
        continue
    fn = f.get("function_name", "")
    fn = "|".join(fn.splitlines())[:42] if fn else "-"
    print(
        f"{f['biomarker_type']:22} {f['severity']:8} "
        f"{f['file_path']:36} {fn:44} "
        f"[{float(f.get('health_impact', 0)):.3f}] {f['reason']}"
    )
