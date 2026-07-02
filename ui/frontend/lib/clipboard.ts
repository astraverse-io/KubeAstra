/**
 * Safely copy text to clipboard, supporting both modern secure contexts (HTTPS, localhost)
 * and older non-secure contexts (HTTP over corporate IPs) using document.execCommand fallback.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof window === "undefined") return false;

  // Use modern Clipboard API if available and in secure context
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.warn("Modern Clipboard API writeText failed, falling back: ", err);
    }
  }

  // Fallback for non-secure HTTP contexts using temporary textarea
  try {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    
    // Position out of viewport and make invisible to avoid visual jump
    textArea.style.position = "fixed";
    textArea.style.top = "0";
    textArea.style.left = "0";
    textArea.style.width = "2em";
    textArea.style.height = "2em";
    textArea.style.padding = "0";
    textArea.style.border = "none";
    textArea.style.outline = "none";
    textArea.style.boxShadow = "none";
    textArea.style.background = "transparent";
    
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    
    const successful = document.execCommand("copy");
    document.body.removeChild(textArea);
    
    return successful;
  } catch (err) {
    console.error("Fallback copy to clipboard failed: ", err);
    return false;
  }
}
