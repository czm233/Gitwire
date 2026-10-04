/** 统一 diff 的着色渲染：+绿 -红 @@青，其余按上下文/meta 淡化 */

type Line = { kind: "add" | "del" | "hunk" | "meta" | "ctx"; text: string };

function parsePatch(patch: string): Line[] {
  return patch.split("\n").map((raw) => {
    if (raw.startsWith("+++") || raw.startsWith("---") || raw.startsWith("diff --git") || raw.startsWith("index ")) {
      return { kind: "meta", text: raw };
    }
    if (raw.startsWith("@@")) return { kind: "hunk", text: raw };
    if (raw.startsWith("+")) return { kind: "add", text: raw };
    if (raw.startsWith("-")) return { kind: "del", text: raw };
    return { kind: "ctx", text: raw };
  });
}

export default function DiffView({ patch }: { patch: string }) {
  const lines = parsePatch(patch);
  return (
    <div className="diff">
      {lines.map((l, i) => (
        <div key={i} className={`dl ${l.kind === "ctx" ? "" : l.kind}`}>
          {l.text || " "}
        </div>
      ))}
    </div>
  );
}
