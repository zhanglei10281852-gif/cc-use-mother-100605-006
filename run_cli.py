from road_permits.core import Permit, summarize
from road_permits.demo import run_demo


if __name__ == "__main__":
    record = Permit.create("demo", "v1", "draft", "operator", {"场景": "登记测试许可并生成许可摘要"})
    print(summarize(record))
    print("=" * 60)
    for line in run_demo():
        print(line)
