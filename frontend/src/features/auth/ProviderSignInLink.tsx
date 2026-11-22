import { ArrowUpRight, ShieldCheck } from "lucide-react";
import { useState } from "react";

import type { LoginScreenMethod } from "../../shared/api/auth";
import { loginMethodLabel, loginMethodStyle } from "./loginMethodBranding";

type Props = {
  method: LoginScreenMethod;
  language: string;
  next: string;
  rememberMe: boolean;
};

export function ProviderSignInLink({ method, language, next, rememberMe }: Props) {
  const [failedIconUrl, setFailedIconUrl] = useState<string | null>(null);
  const iconUrl = method.icon_url;
  const appearance = method.sign_in_button;
  const query = new URLSearchParams({ next, remember_me: String(rememberMe) });

  return (
    <a
      className="icon-button auth-login-action auth-login-provider"
      href={`${method.start_url}?${query.toString()}`}
      style={{
        ...loginMethodStyle(method),
        borderColor: appearance?.border_color,
        borderRadius: appearance?.border_radius,
      }}
    >
      {iconUrl && iconUrl !== failedIconUrl ? (
        <img src={iconUrl} width={20} height={20} alt="" onError={() => setFailedIconUrl(iconUrl)} />
      ) : (
        <ShieldCheck size={20} aria-hidden="true" />
      )}
      <span>{loginMethodLabel(method, language)}</span>
      <ArrowUpRight size={16} aria-hidden="true" />
    </a>
  );
}
