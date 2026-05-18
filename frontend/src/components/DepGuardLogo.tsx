import logoSrc from "@/assets/logo.png";
import { cn } from "@/lib/utils";

type DepGuardLogoProps = {
  className?: string;
  title?: string;
};

export function DepGuardLogo({ className, title = "DepGuard AI logo" }: DepGuardLogoProps) {
  return (
    <img
      alt={title}
      className={cn("size-11 shrink-0 object-contain", className)}
      draggable={false}
      src={logoSrc}
    />
  );
}
