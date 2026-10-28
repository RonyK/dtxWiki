import { LoadingIndicator } from "./LoadingIndicator";

type RouteLoadingProps = {
  label?: string;
};

export function RouteLoading({ label = "Loading" }: RouteLoadingProps) {
  return (
    <LoadingIndicator aria-label={label} className="route-loading" size={18}>
      {label}
    </LoadingIndicator>
  );
}
