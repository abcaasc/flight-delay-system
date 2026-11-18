import names from "../data/airports.zh-CN.json";

// BTS 出港代码对应的中文展示名。基础标签来自 Wikidata（CC0），
// 按数仓机场名称校订，包含十年内停用的机场。新代码保留数据源原名。
export const airportNames: Readonly<Record<string, string>> = names;

export function airportName(code: string, original: string) {
  return airportNames[code] ?? original;
}
