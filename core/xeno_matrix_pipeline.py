import json
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Any

class XenoMatrixDispatcher:
    """
    OpenMatrix High-Density Latent Swarm Pipeline
    Incorporates:
    - latent-semantics (RLM Context)
    - xeno-compiler (IR compression)
    - agent-compact-protocol (internal high-density msg passing)
    - shared-board (cross-agent finding bus)
    - readable-gate-envelope (human-readable final delivery)
    """
    def __init__(self, openai_client=None, model_name: str = "gpt-4o"):
        self.client = openai_client
        self.model_name = model_name
        configured_xlat = os.getenv("OPENMATRIX_XLAT_PATH", "").strip()
        self.xlat_path = Path(configured_xlat) if configured_xlat else None
        self.base_dir = Path(os.getenv("OPENMATRIX_SEMANTIC_INDEX", ".openmatrix/semantic-index"))

    def _xlat_command(self) -> list[str]:
        if self.xlat_path is None or not self.xlat_path.is_file():
            raise RuntimeError(
                "Matrix semantic search is not configured. Set OPENMATRIX_XLAT_PATH "
                "to the latent-semantics xlat.py script."
            )
        return [sys.executable, str(self.xlat_path)]

    async def index_directory(self, target_dir: str, index_name: str) -> bool:
        cmd = self._xlat_command() + [
            "index", target_dir, "--name", index_name, "--base", str(self.base_dir)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Matrix indexing failed: {detail or 'unknown error'}")
        return True

    async def compile_to_xeno(self, user_intent: str) -> str:
        """agent-compact-protocol: 极高密度意图压缩"""
        if self.client is None:
            raise RuntimeError("Matrix mode requires an LLM client")
        resp = await self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Compile the user's request into concise, auditable Xeno IR with "
                        "[OBJ], [ACT], and [OUT] sections. Preserve the requested entities "
                        "and output contract. Return only the IR."
                    ),
                },
                {"role": "user", "content": user_intent}
            ],
            temperature=0.0
        )
        return resp.choices[0].message.content.strip()

    async def semantic_recall(self, query: str, index_name: str, top_k: int = 5) -> List[str]:
        cmd = self._xlat_command() + [
            "search", query,
            "--name", index_name, "--base", str(self.base_dir),
            "--top", str(top_k), "--json"
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Matrix semantic search failed: {detail or 'unknown error'}")
        try:
            results = json.loads(stdout.decode('utf-8'))
            return [res.get("text", "") for res in results if res.get("text")]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Semantic search returned invalid JSON") from exc

    async def _map_agent_task(self, chunk: str, xeno_ir: str, shared_board: List[Any], task_id: int) -> Dict[str, Any]:
        """shared-board: 代理将发现线索注入黑板"""
        if self.client is None:
            raise RuntimeError("Matrix mode requires an LLM client")
        prompt = f"IR: {xeno_ir}\nDATA: {chunk}"
        resp = await self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": "Return only valid JSON findings grounded in DATA."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()
        result = json.loads(content)
        finding = f"Task {task_id} produced a grounded finding"
        shared_board.append(finding)
        return {"chunk_data": chunk[:30] + "...", "result": result}

    async def apply_readable_gate(self, aggregated_results: List[Dict], shared_board: List[Any]) -> Dict[str, Any]:
        """readable-gate-envelope: 对外人话交付，带因果信号与闸门判决"""
        if self.client:
            try:
                prompt = f"Results: {aggregated_results}\nBoard: {shared_board}"
                resp = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Return only valid JSON with exactly these top-level fields: "
                                "CLAIMS (string array), UNKNOWNS (string array), GATES "
                                "(string-to-string object), and VERDICT (string). Base every "
                                "claim on Results and report uncertainty instead of guessing."
                            ),
                        },
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0
                )
                content = resp.choices[0].message.content.strip()
                if content.startswith("```json"):
                    content = content[7:-3].strip()
                elif content.startswith("```"):
                    content = content[3:-3].strip()
                return json.loads(content)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Readable gate returned invalid JSON") from exc
        raise RuntimeError("Matrix mode requires an LLM client")

    async def run_pipeline(self, index_name: str, user_intent: str, top_k: int = 5) -> Dict[str, Any]:
        print("[XenoMatrix] 1. agent-compact-protocol: 编译极简 IR")
        xeno_ir = await self.compile_to_xeno(user_intent)
        print(f"  -> IR: {xeno_ir}")

        print("[XenoMatrix] 2. latent-semantics: 潜空间精准召回")
        chunks = await self.semantic_recall(user_intent, index_name, top_k)
        if not chunks:
            return {"status": "error", "message": "未召回任何切片。"}

        print(f"[XenoMatrix] 3. shared-board & agent-teams: 扇出子代理群 (召回 {len(chunks)} 块切片)")
        shared_board = [] # J-Space 留言板
        tasks = [self._map_agent_task(chunk, xeno_ir, shared_board, i) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks)

        print("[XenoMatrix] 4. readable-gate-envelope: 过闸拦截与人话封装")
        final_envelope = await self.apply_readable_gate(results, shared_board)
        
        return {
            "xeno_ir": xeno_ir,
            "processed_chunks": len(chunks),
            "shared_board_findings": shared_board,
            "envelope": final_envelope,
            "raw_findings": results
        }
