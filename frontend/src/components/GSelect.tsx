import { cn } from "@/lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export type GOption = { value: string; label: string };

/** 空值哨兵：业务层用 "" 表示「全部」，Radix 不接受空串 item value，进出各转一次 */
const EMPTY = "__empty__";

/**
 * 终端风下拉框：视觉对齐原 .filter-bar select（mono 12px / 5px 10px 内边距 / 120px 最小宽），
 * 行为换成标准 Radix 下拉——键盘上下选、字母快选、点外关闭、弹层跟随主题。
 * placeholder 同时作为列表第一项（等于原「全部X」选项）。
 */
export function GSelect({
  value,
  onChange,
  options,
  placeholder,
  className,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: GOption[];
  placeholder?: string;
  className?: string;
  ariaLabel?: string;
}) {
  const itemCls =
    "py-1.5 pr-7 pl-2 text-xs focus:bg-accent focus:text-accent-foreground";
  return (
    <Select
      value={value === "" ? EMPTY : value}
      onValueChange={(v) => onChange(v === EMPTY || v === undefined ? "" : v)}
    >
      <SelectTrigger
        aria-label={ariaLabel || placeholder}
        className={cn(
          "h-[29px] data-[size=default]:h-[29px] min-w-[120px] justify-between gap-1.5 rounded-md border-border bg-transparent px-2.5 py-[5px] font-mono text-xs text-foreground data-[placeholder]:text-foreground shadow-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20 dark:bg-transparent dark:hover:bg-transparent",
          className
        )}
      >
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent
        position="popper"
        sideOffset={4}
        className="rounded-md border-border font-mono text-xs"
      >
        {placeholder !== undefined && (
          <SelectItem value={EMPTY} className={itemCls}>
            {placeholder}
          </SelectItem>
        )}
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value} className={itemCls}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
