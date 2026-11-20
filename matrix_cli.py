import asyncio
import argparse
import json
from core.xeno_matrix_pipeline import XenoMatrixDispatcher
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=str)
    parser.add_argument("--index", type=str, required=True)
    parser.add_argument("--query", type=str, required=True)
    args = parser.parse_args()

    # Pass None to trigger the mock fallback cleanly without httpx crash
    dispatcher = XenoMatrixDispatcher(openai_client=None)

    if args.dir:
        print(f"[*] 启动潜空间打标: {args.dir} -> {args.index}")
        await dispatcher.index_directory(args.dir, args.index)

    print(f"[*] 启动矩阵挖掘: Query='{args.query}'")
    result = await dispatcher.run_pipeline(args.index, args.query, top_k=3)
    
    print("\n" + "="*50)
    print("🚀 矩阵挖掘最终报告 (Reduce Result)")
    print("="*50)
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
