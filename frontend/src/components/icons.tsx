/** Minimal hand-authored inline icons — no icon-library dependency for eight glyphs. */
import type { ReactNode } from "react";

type IconProps = { children: ReactNode };
function Svg({ children }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
      {children}
    </svg>
  );
}

export const GridIcon = () => (
  <Svg>
    <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
    <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
    <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
    <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
  </Svg>
);

export const HomeIcon = () => (
  <Svg>
    <path d="M4 11.5 12 4l8 7.5" />
    <path d="M6 10v9h12v-9" />
  </Svg>
);

export const UploadIcon = () => (
  <Svg>
    <path d="M12 15V4" />
    <path d="M7.5 8.5 12 4l4.5 4.5" />
    <path d="M4.5 15v3.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V15" />
  </Svg>
);

export const GraphIcon = () => (
  <Svg>
    <circle cx="6" cy="6" r="2.4" />
    <circle cx="18" cy="6" r="2.4" />
    <circle cx="12" cy="18" r="2.4" />
    <path d="M7.9 7.4 10.4 16" />
    <path d="M16.1 7.4 13.6 16" />
    <path d="M8.4 6h7.2" />
  </Svg>
);

export const FlagIcon = () => (
  <Svg>
    <path d="M6 3.5v17" />
    <path d="M6 4.5h11l-3 4 3 4H6" />
  </Svg>
);

export const GearIcon = () => (
  <Svg>
    <circle cx="12" cy="12" r="3.2" />
    <path d="M12 3.5v2.2M12 18.3v2.2M20.5 12h-2.2M5.7 12H3.5M17.7 6.3l-1.5 1.5M7.8 16.2l-1.5 1.5M17.7 17.7l-1.5-1.5M7.8 7.8 6.3 6.3" />
  </Svg>
);

export const ShieldIcon = () => (
  <Svg>
    <path d="M12 3.5 5 6v6c0 4.2 3 7.4 7 8.5 4-1.1 7-4.3 7-8.5V6z" />
    <path d="M9.2 12l2 2 3.6-3.8" />
  </Svg>
);

export const CheckIcon = () => (
  <Svg>
    <path d="M4.5 12.5 9.5 17.5 19.5 6.5" />
  </Svg>
);

export const ExportIcon = () => (
  <Svg>
    <path d="M12 4v10.5" />
    <path d="M7.5 11 12 15.5 16.5 11" />
    <path d="M4.5 15v3.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V15" />
  </Svg>
);

export const EntityIcon = () => (
  <Svg>
    <circle cx="8" cy="8" r="2.6" />
    <circle cx="16.5" cy="7" r="2.1" />
    <circle cx="13" cy="16.5" r="2.6" />
    <path d="M10.4 8.8 13 14" />
    <path d="M10.6 7.6 14.4 7.2" />
    <path d="M15.8 9 14.2 14" />
  </Svg>
);

export const GlobeIcon = () => (
  <Svg>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M3.5 12h17" />
    <path d="M12 3.5c2.6 2.4 3.6 5.3 3.6 8.5s-1 6.1-3.6 8.5c-2.6-2.4-3.6-5.3-3.6-8.5s1-6.1 3.6-8.5Z" />
  </Svg>
);
