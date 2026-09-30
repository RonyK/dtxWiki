import { Spinner } from "./Spinner";

type RouteLoadingProps = {
  label?: string;
};

export function RouteLoading({ label = "Loading" }: RouteLoadingProps) {
  return (
    <div className="route-loading" role="status" aria-live="polite" aria-label={label}>
      <Spinner decorative className="route-loading-icon" size={18} />
      <span>{label}</span>
    </div>
  );
}
