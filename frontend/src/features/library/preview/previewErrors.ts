export type PreviewRendererFailureKind =
  | "authentication"
  | "codec"
  | "runtime";

export class PreviewRendererFailure extends Error {
  readonly kind: PreviewRendererFailureKind;

  constructor(kind: PreviewRendererFailureKind) {
    super("Preview renderer unavailable");
    this.name = "PreviewRendererFailure";
    this.kind = kind;
  }
}

export function previewRendererFailureKind(value: unknown) {
  return value instanceof PreviewRendererFailure ? value.kind : null;
}
