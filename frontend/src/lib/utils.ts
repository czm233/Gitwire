import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn 约定的类名合并工具：条件类名 + Tailwind 原子类去重 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
