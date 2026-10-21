import { Copy } from "lucide-react";

export default { title: "Shared/Icon button sizes" };

export function Sizes() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
      <button className="icon-button" type="button" aria-label="Standard action">
        <Copy size={14} aria-hidden="true" />
      </button>
      <button className="icon-button compact-icon-button" type="button" aria-label="Compact action">
        <Copy size={14} aria-hidden="true" />
      </button>
      <button className="icon-button icon-button--small" type="button" aria-label="Copy ID">
        <Copy size={14} aria-hidden="true" />
      </button>
      <span>Small: 28 × 28px, including mobile and tablet</span>
    </div>
  );
}
