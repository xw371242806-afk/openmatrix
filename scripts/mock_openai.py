import json
from http.server import BaseHTTPRequestHandler, HTTPServer

DOCS = {
    "92.3": {"answer": "Quarterly revenue was $92.3 billion, up 21.5% YoY.",
             "quote": "The global cloud infrastructure market reached $92.3 billion in quarterly revenue in Q3 FY2026, up 21.5% year over year."},
    "4.87": {"answer": "Global EV sales were 4.87 million units, 22.1% of all new vehicle sales.",
             "quote": "Global EV sales totaled 4.87 million units in Q3 2026, representing 22.1% of all new vehicle sales worldwide."},
}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        prompt = body["messages"][-1]["content"]
        hit = None
        for key, val in DOCS.items():
            if key in prompt:
                hit = val
                break
        if hit:
            out = [{"question": "What was the total quarterly volume/revenue?", "answer": hit["answer"], "quote": hit["quote"]}]
        else:
            out = [{"question": "?", "answer": "Not found in the document.", "quote": "No matching passage."}]
        res = {"id": "m1", "object": "chat.completion", "created": 0,
               "model": body.get("model", "mock"),
               "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(out)}, "finish_reason": "stop"}]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(res).encode())

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 54321), H).serve_forever()
