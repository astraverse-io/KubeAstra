import type { Metadata } from "next";
import { IBM_Plex_Sans, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";

const ibmPlexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-ibm-plex-sans",
});

const ibmPlexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-ibm-plex-mono",
});

export const metadata: Metadata = {
  title: "KubeAstra — Kubernetes AI Assistant",
  description: "AI-powered Kubernetes assistant — ask a question or paste an error.",
};

// Runs synchronously in <head> before hydration to set data-theme from
// localStorage. Legacy "mission-control" values (from earlier builds)
// coerce to "dark" — the mission-control aesthetic is now baked into
// the dark palette, not a separate theme.
const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem('theme');if(t==='mission-control'){t='dark';localStorage.setItem('theme','dark');}if(t==='dark'||t==='light'){document.documentElement.setAttribute('data-theme',t);}}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className={`${ibmPlexSans.variable} ${ibmPlexMono.variable}`}>
        {children}
      </body>
    </html>
  );
}
