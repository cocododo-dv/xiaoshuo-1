import React from "react";

/* global React */

/* ==========================================================
   Inline SVG icons — feather-style, 1.6 stroke
   Each accepts {size?} prop. Default 18.
   Exposed on window so all screen files can use them.
   ========================================================== */

const Icon = ({ d, size = 18, fill = "none", strokeWidth = 1.6, viewBox = "0 0 24 24", children, ...rest }) => (
  <svg
    width={size}
    height={size}
    viewBox={viewBox}
    fill={fill}
    stroke="currentColor"
    strokeWidth={strokeWidth}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...rest}
  >
    {d ? <path d={d} /> : children}
  </svg>
);

const I = {
  // Nav
  Home: (p) => <Icon {...p}><path d="M3 11l9-8 9 8" /><path d="M5 10v10h14V10" /></Icon>,
  Snowflake: (p) => <Icon {...p}><path d="M12 2v20" /><path d="M2 12h20" /><path d="M5 5l14 14" /><path d="M19 5L5 19" /><path d="M9 5l3-3 3 3" /><path d="M9 19l3 3 3-3" /><path d="M5 9l-3 3 3 3" /><path d="M19 9l3 3-3 3" /></Icon>,
  Pen: (p) => <Icon {...p}><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></Icon>,
  Inbox: (p) => <Icon {...p}><path d="M22 12h-6l-2 3h-4l-2-3H2" /><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></Icon>,
  Library: (p) => <Icon {...p}><path d="M4 4h3v16H4z" /><path d="M9 4h3v16H9z" /><path d="M15 5l3-1 4 14-3 1z" /></Icon>,
  // Tools / minor
  Settings: (p) => <Icon {...p}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></Icon>,
  Trash: (p) => <Icon {...p}><path d="M3 6h18" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6" /><path d="M14 11v6" /><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></Icon>,
  Search: (p) => <Icon {...p}><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.35-4.35" /></Icon>,
  GripVertical: (p) => <Icon {...p}><circle cx="9" cy="6" r="1.1" fill="currentColor" stroke="none" /><circle cx="9" cy="12" r="1.1" fill="currentColor" stroke="none" /><circle cx="9" cy="18" r="1.1" fill="currentColor" stroke="none" /><circle cx="15" cy="6" r="1.1" fill="currentColor" stroke="none" /><circle cx="15" cy="12" r="1.1" fill="currentColor" stroke="none" /><circle cx="15" cy="18" r="1.1" fill="currentColor" stroke="none" /></Icon>,
  Bell: (p) => <Icon {...p}><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></Icon>,
  Plus: (p) => <Icon {...p}><path d="M12 5v14" /><path d="M5 12h14" /></Icon>,
  List: (p) => <Icon {...p}><line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" /></Icon>,
  ChevronRight: (p) => <Icon {...p}><path d="M9 18l6-6-6-6" /></Icon>,
  ChevronDown: (p) => <Icon {...p}><path d="M6 9l6 6 6-6" /></Icon>,
  ChevronUp: (p) => <Icon {...p}><path d="M18 15l-6-6-6 6" /></Icon>,
  Scissors: (p) => <Icon {...p}><circle cx="6" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M20 4L8.12 15.88" /><path d="M14.47 14.48L20 20" /><path d="M8.12 8.12L12 12" /></Icon>,
  ChevronLeft: (p) => <Icon {...p}><path d="M15 18l-6-6 6-6" /></Icon>,
  Check: (p) => <Icon {...p}><path d="M20 6L9 17l-5-5" /></Icon>,
  CheckCircle: (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><path d="M8 12l3 3 5-6" /></Icon>,
  Circle: (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /></Icon>,
  Dot: (p) => <Icon {...p}><circle cx="12" cy="12" r="3" fill="currentColor" /></Icon>,
  Sparkles: (p) => <Icon {...p}><path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5z" /><path d="M19 14l.7 2.1 2.3.4-2.3.4-.7 2.1-.7-2.1-2.3-.4 2.3-.4z" /><path d="M5 4l.4 1.2L7 5.6 5.4 6 5 7.2 4.6 6 3 5.6l1.6-.4z" /></Icon>,
  Eye: (p) => <Icon {...p}><path d="M2 12s4-8 10-8 10 8 10 8-4 8-10 8-10-8-10-8z" /><circle cx="12" cy="12" r="3" /></Icon>,
  Edit: (p) => <Icon {...p}><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></Icon>,
  Save: (p) => <Icon {...p}><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" /><path d="M17 21v-8H7v8" /><path d="M7 3v5h8" /></Icon>,
  Clock: (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" /></Icon>,
  BookOpen: (p) => <Icon {...p}><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" /></Icon>,
  Layers: (p) => <Icon {...p}><path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" /></Icon>,
  Users: (p) => <Icon {...p}><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></Icon>,
  MapPin: (p) => <Icon {...p}><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" /><circle cx="12" cy="10" r="3" /></Icon>,
  Compass: (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><path d="M16.24 7.76l-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12z" /></Icon>,
  Star: (p) => <Icon {...p}><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" /></Icon>,
  AlertTriangle: (p) => <Icon {...p}><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><path d="M12 9v4" /><path d="M12 17h.01" /></Icon>,
  Info: (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><path d="M12 16v-4" /><path d="M12 8h.01" /></Icon>,
  FileText: (p) => <Icon {...p}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><path d="M8 13h8" /><path d="M8 17h8" /><path d="M8 9h2" /></Icon>,
  Layout: (p) => <Icon {...p}><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" /></Icon>,
  Filter: (p) => <Icon {...p}><path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" /></Icon>,
  More: (p) => <Icon {...p}><circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none" /></Icon>,
  Moon: (p) => <Icon {...p}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></Icon>,
  Sun: (p) => <Icon {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2" /><path d="M12 20v2" /><path d="M4.93 4.93l1.41 1.41" /><path d="M17.66 17.66l1.41 1.41" /><path d="M2 12h2" /><path d="M20 12h2" /><path d="M4.93 19.07l1.41-1.41" /><path d="M17.66 6.34l1.41-1.41" /></Icon>,
  ArrowRight: (p) => <Icon {...p}><path d="M5 12h14" /><path d="M12 5l7 7-7 7" /></Icon>,
  Play: (p) => <Icon {...p}><path d="M5 3l14 9-14 9V3z" fill="currentColor" /></Icon>,
  Refresh: (p) => <Icon {...p}><path d="M23 4v6h-6" /><path d="M1 20v-6h6" /><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" /><path d="M20.49 15A9 9 0 0 1 5.64 18.36L1 14" /></Icon>,
  Wand: (p) => <Icon {...p}><path d="M15 4l5 5" /><path d="M3 21l12-12" /><path d="M18 2l1 3 3 1-3 1-1 3-1-3-3-1 3-1z" /></Icon>,
  Type: (p) => <Icon {...p}><path d="M4 7V4h16v3" /><path d="M9 20h6" /><path d="M12 4v16" /></Icon>,
  Tag: (p) => <Icon {...p}><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" /><path d="M7 7h.01" /></Icon>,
  Folder: (p) => <Icon {...p}><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></Icon>,
  X: (p) => <Icon {...p}><path d="M18 6L6 18" /><path d="M6 6l12 12" /></Icon>,
  PanelLeft: (p) => <Icon {...p}><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 3v18" /></Icon>,
  Microscope: (p) => <Icon {...p}><path d="M6 18h8" /><path d="M3 22h18" /><path d="M14 22a7 7 0 1 0 0-14h-1" /><path d="M9 14h2" /><path d="M9 12a2 2 0 0 1-2-2V6h4v4a2 2 0 0 1-2 2z" /><path d="M12 6V3a1 1 0 0 0-1-1H9a1 1 0 0 0-1 1v3" /></Icon>,
  Radar: (p) => <Icon {...p}><path d="M19.07 4.93A10 10 0 0 0 6.99 3.34" /><path d="M4 6h.01" /><path d="M2.29 9.62A10 10 0 1 0 21.31 8.35" /><path d="M16.24 7.76A6 6 0 1 0 17.95 12" /><path d="M12 12h-.01" /><path d="M12 12 2 22" /></Icon>,
  UploadCloud: (p) => <Icon {...p}><path d="M16 16l-4-4-4 4" /><path d="M12 12v9" /><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" /><path d="M16 16l-4-4-4 4" /></Icon>,
  FileInput: (p) => <Icon {...p}><path d="M4 22h14a2 2 0 0 0 2-2V7.5L14.5 2H6a2 2 0 0 0-2 2v4" /><polyline points="14 2 14 8 20 8" /><path d="M2 15h10" /><path d="M9 18l3-3-3-3" /></Icon>,
  ShieldCheck: (p) => <Icon {...p}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><path d="M9 12l2 2 4-4" /></Icon>,
  Lock: (p) => <Icon {...p}><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></Icon>,
  Unlock: (p) => <Icon {...p}><rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 9.9-1" /></Icon>,
  Cpu: (p) => <Icon {...p}><rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" /><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" /></Icon>,
  UserCheck: (p) => <Icon {...p}><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M16 11l2 2 4-4" /></Icon>,
  Coins: (p) => <Icon {...p}><circle cx="8" cy="8" r="6" /><path d="M18.09 10.37A6 6 0 1 1 10.34 18" /><path d="M7 6h1v4" /><path d="M16.71 13.88l.71.71-2.83 2.83" /></Icon>,
  Database: (p) => <Icon {...p}><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0 0 18 0V5" /><path d="M3 12a9 3 0 0 0 18 0" /></Icon>,
  Activity: (p) => <Icon {...p}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></Icon>,
  Zap: (p) => <Icon {...p}><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></Icon>,
  GitBranch: (p) => <Icon {...p}><line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" /></Icon>,
  Link: (p) => <Icon {...p}><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" /><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" /></Icon>,
  Grid: (p) => <Icon {...p}><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /></Icon>,
  Target: (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="6" /><circle cx="12" cy="12" r="2" /></Icon>,
  Flask: (p) => <Icon {...p}><path d="M9 3h6" /><path d="M10 3v6.5L4.5 19a1.5 1.5 0 0 0 1.3 2.3h12.4a1.5 1.5 0 0 0 1.3-2.3L14 9.5V3" /><path d="M7 15h10" /></Icon>,
  Sliders: (p) => <Icon {...p}><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></Icon>,
  Ban: (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><line x1="4.93" y1="4.93" x2="19.07" y2="19.07" /></Icon>,
  Quote: (p) => <Icon {...p}><path d="M3 21c3 0 7-1 7-8V5c0-1.25-.756-2.017-2-2H4c-1.25 0-2 .75-2 1.972V11c0 1.25.75 2 2 2 1 0 1 0 1 1v1c0 1-1 2-2 2s-1 .008-1 1.031V20c0 1 0 1 1 1z" /><path d="M15 21c3 0 7-1 7-8V5c0-1.25-.757-2.017-2-2h-4c-1.25 0-2 .75-2 1.972V11c0 1.25.75 2 2 2h.75c0 2.25.25 4-2.75 4v3c0 1 0 1 1 1z" /></Icon>,
  Beaker: (p) => <Icon {...p}><path d="M4.5 3h15" /><path d="M6 3v16a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V3" /><path d="M6 14h12" /></Icon>,
  Download: (p) => <Icon {...p}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></Icon>,
};

export { I, Icon };
