import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { useExplicitInertRef } from "./useExplicitInertRef";

type BottomSheetProps = ComponentPropsWithoutRef<"aside"> & {
  children: ReactNode;
  label: string;
  mobileLayout: boolean;
  mobileOpen: boolean;
};

export const BottomSheet = forwardRef<HTMLElement, BottomSheetProps>(
  function BottomSheet(
    { children, className = "", label, mobileLayout, mobileOpen, ...props },
    ref,
  ) {
    const { t } = useTranslation("shell");
    const hiddenOnMobile = mobileLayout && !mobileOpen;
    const inertRef = useExplicitInertRef(ref, hiddenOnMobile);

    return (
      <aside
        {...props}
        aria-hidden={hiddenOnMobile ? "true" : undefined}
        aria-label={
          mobileLayout
            ? t("panel.sheet", { defaultValue: `${label} sheet`, label })
            : undefined
        }
        aria-modal={mobileLayout && mobileOpen ? "true" : undefined}
        className={["inspector-panel", className].filter(Boolean).join(" ")}
        data-mobile-open={mobileOpen ? "true" : "false"}
        ref={inertRef}
        role={mobileLayout ? "dialog" : undefined}
        tabIndex={mobileLayout ? -1 : undefined}
      >
        {children}
      </aside>
    );
  },
);
