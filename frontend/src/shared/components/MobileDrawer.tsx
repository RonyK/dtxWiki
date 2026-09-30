import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { useExplicitInertRef } from "./useExplicitInertRef";

type MobileDrawerProps = ComponentPropsWithoutRef<"div"> & {
  children: ReactNode;
  label: string;
  mobileLayout: boolean;
  mobileOpen: boolean;
};

export const MobileDrawer = forwardRef<HTMLDivElement, MobileDrawerProps>(
  function MobileDrawer(
    { children, className = "", label, mobileLayout, mobileOpen, ...props },
    ref,
  ) {
    const { t } = useTranslation("shell");
    const hiddenOnMobile = mobileLayout && !mobileOpen;
    const inertRef = useExplicitInertRef(ref, hiddenOnMobile);

    return (
      <div
        {...props}
        aria-hidden={hiddenOnMobile ? "true" : undefined}
        aria-label={
          mobileLayout
            ? t("panel.drawer", { defaultValue: `${label} drawer`, label })
            : undefined
        }
        aria-modal={mobileLayout && mobileOpen ? "true" : undefined}
        className={["workspace-left-panel", className].filter(Boolean).join(" ")}
        data-mobile-open={mobileOpen ? "true" : "false"}
        ref={inertRef}
        role={mobileLayout ? "dialog" : undefined}
        tabIndex={mobileLayout ? -1 : undefined}
      >
        {children}
      </div>
    );
  },
);
