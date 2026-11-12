/** A CSS hex or rgb() colour as WebGL channels in 0..1. */
export function rgb(color: string): [number, number, number] {
  const hex = color.trim().match(/^#([\da-f]{3}|[\da-f]{6})$/i);
  if (hex) {
    const value = hex[1].length === 3
      ? [...hex[1]].map((digit) => digit + digit).join("")
      : hex[1];
    return [0, 2, 4].map((start) => Number.parseInt(value.slice(start, start + 2), 16) / 255) as [number, number, number];
  }
  const functional = color.trim().match(/^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
  if (functional) return [1, 2, 3].map((index) => Math.min(255, Number(functional[index])) / 255) as [number, number, number];
  // A forced-colors system keyword is not a WebGL color; use high-contrast ink.
  return [0, 0, 0];
}
