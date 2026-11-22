"""启动入口：初始化数据库（可选载入演示数据）后运行 Web 服务。"""
import os

from grr.app import create_app
from grr.seed import seed_demo


def main():
    db_path = os.environ.get(
        "DATABASE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "grr.db")
    )
    app = create_app({"DATABASE": db_path})
    if os.environ.get("DEMO_SEED") == "1" and seed_demo(db_path):
        print("已载入演示数据：3 名检验员 × 5 件样件，读数齐全，现行报告已生成。")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
