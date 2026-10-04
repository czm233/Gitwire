import { useEffect, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ gfm: true, breaks: false });

let mermaidReady: Promise<typeof import("mermaid")> | null = null;
function loadMermaid() {
  if (!mermaidReady) {
    mermaidReady = import("mermaid").then((m) => {
      m.default.initialize({
        startOnLoad: false,
        theme: "dark",
        themeVariables: {
          primaryColor: "#12202b",
          primaryTextColor: "#c8d6e0",
          primaryBorderColor: "#2a3b4a",
          lineColor: "#3d8f63",
          textColor: "#c8d6e0",
          fontSize: "13px",
        },
      });
      return m;
    });
  }
  return mermaidReady;
}

/** markdown 渲染 + mermaid 图替换，失败的图保留原码块 */
export default function Markdown({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [html, setHtml] = useState("");

  useEffect(() => {
    setHtml(DOMPurify.sanitize(marked.parse(text, { async: false }) as string));
  }, [text]);

  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    let alive = true;
    const blocks = Array.from(root.querySelectorAll("pre code.language-mermaid"));
    if (blocks.length === 0) return;
    loadMermaid().then((m) => {
      if (!alive) return;
      blocks.forEach((block, i) => {
        const src = block.textContent || "";
        const id = `mmd-${Date.now()}-${i}`;
        m.default
          .render(id, src)
          .then(({ svg }) => {
            if (!alive) return;
            const holder = document.createElement("div");
            holder.className = "mermaid-svg";
            holder.innerHTML = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } });
            block.parentElement?.replaceWith(holder);
          })
          .catch(() => {
            /* 渲染失败保留源码块 */
          });
      });
    });
    return () => {
      alive = false;
    };
  }, [html]);

  return <div ref={ref} className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}
