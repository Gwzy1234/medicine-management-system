"""仅供浏览器联调的临时服务，不使用 medical.db，不加入正式启动入口。

运行：.venv/bin/python -B -m tests.frontend_preview --root 临时目录 --port 空闲端口
测试账号均为虚构；页面测试结束后由调用者删除整个临时目录。
"""

import argparse
import asyncio
import json
import secrets
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Settings
from backend.main import create_app
from medicine_db import BASE_DIR, MedicineDB, business_today


PASSWORD = "Fictional-browser-only-2026"


def preview_app(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # 防止误将正式项目根目录用于测试。
    if root.resolve() == BASE_DIR.resolve() or root.resolve().is_relative_to(BASE_DIR.resolve()):
        raise ValueError("浏览器夹具必须位于项目目录之外的临时目录")
    db = MedicineDB(root / "frontend-test.sqlite3")
    fresh = not db.path.exists()
    db.init_db()
    if fresh:
        admin = db.create_first_admin("browser_admin", PASSWORD)["id"]
        staff = db.create_user(admin, "browser_staff", PASSWORD, "staff")["id"]
        db.create_user(admin, "browser_viewer", PASSWORD, "viewer")
        today = business_today()
        fixtures = []
        for name, count, days, location in [
            ("肾上腺素注射液", 3, 365, "CT 室 · 急救柜 A 层"),
            ("阿托品注射液", 2, 220, "MRI 准备室 · 急救柜"),
            ("盐酸利多卡因注射液", 1, 280, "介入室 · 药品柜 B 层"),
            ("地塞米松磷酸钠注射液", 1, 10, "CT 室 · 急救柜 B 层"),
            ("葡萄糖注射液", 1, -1, "虚构过期药品区"),
            ("氯化钠注射液", 1, 120, "虚构停用药品区"),
        ]:
            for _ in range(count):
                row = db.add_medicine(name, (today + timedelta(days=days)).isoformat(), admin, location)
                fixtures.append(row)
                if name == "氯化钠注射液":
                    db.disable_medicine(row["id"], admin)
        db.withdraw_by_nfc(fixtures[4]["nfc_uuid"], staff)
        (root / "fixture.json").write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    secret_path = root / "test-secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(48))
    app = create_app(Settings(db.path, secret_path.read_text()), db)
    app.mount("/__tests", StaticFiles(directory=BASE_DIR / "tests" / "frontend"), name="browser-tests")

    @app.middleware("http")
    async def targeted_failure(request, call_next):
        # 只用于异常分支测试。正常请求始终进入真实认证、业务层与 SQLite。
        fault_path = root / "fault.json"
        if fault_path.exists():
            fault = json.loads(fault_path.read_text())
            if request.url.path == fault.get("path"):
                with (root / "fault-requests.log").open("a") as log:
                    log.write(request.url.path + "\n")
                await asyncio.sleep(fault.get("delay", 0))
                if fault.get("pass_through"):
                    return await call_next(request)
                return JSONResponse(fault.get("body", {"error": {"code": "test_failure", "message": "临时测试故障"}}), status_code=fault.get("status", 503))
        return await call_next(request)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    uvicorn.run(preview_app(arguments.root), host="127.0.0.1", port=arguments.port, log_level="warning")
