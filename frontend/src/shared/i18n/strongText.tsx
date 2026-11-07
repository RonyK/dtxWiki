import { Fragment, type ReactNode } from "react";

/**
 * A translated sentence with its interpolated values set in bold, in the
 * locale's own word order ("{{source}}에 {{target}} 링크" / "Link to
 * {{target}} in {{source}}"). The translation receives private-use markers
 * in place of the values and each marker becomes a <strong> element, so a
 * value such as a page title is always text, never markup.
 */
export function strongText(
  translate: (markers: Record<string, string>) => string,
  values: Record<string, string>,
): ReactNode {
  const names = Object.keys(values);
  const markers = Object.fromEntries(
    names.map((name, index) => [name, String.fromCharCode(0xe000 + index)]),
  );
  const pattern = new RegExp(`([${Object.values(markers).join("")}])`);
  return translate(markers)
    .split(pattern)
    .map((part, index) => {
      const name = names.find((key) => markers[key] === part);
      return <Fragment key={index}>{name ? <strong>{values[name]}</strong> : part}</Fragment>;
    });
}
