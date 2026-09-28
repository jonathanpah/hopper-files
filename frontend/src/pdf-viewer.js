import * as pdfjs from "pdfjs-dist/build/pdf.mjs";

export async function renderPdfPreview({container, source, basePath, onStatus}) {
  pdfjs.GlobalWorkerOptions.workerSrc = `${basePath}assets/pdf.worker.min.mjs`;
  // pdf.js 6: release the document via the loading task (PDFDocumentProxy itself has no destroy method).
  let documentHandle;
  let loadingTask = null;
  const release = () => { loadingTask?.destroy(); loadingTask = null; documentHandle = null; };
  try {
    const response = await fetch(source, {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new Error(response.status === 415 ? "Este arquivo não é um PDF compatível." : "Não foi possível ler o PDF.");
    const bytes = new Uint8Array(await response.arrayBuffer());
    loadingTask = pdfjs.getDocument({
      data: bytes,
      isEvalSupported: false,
      useSystemFonts: true,
      cMapUrl: `${basePath}assets/cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${basePath}assets/standard_fonts/`,
      iccUrl: `${basePath}assets/iccs/`,
      wasmUrl: `${basePath}assets/wasm/`,
    });
    documentHandle = await loadingTask.promise;
    // The canvas is rendered at the CSS-controlled page width (100% width, auto height), so its
    // resolution is computed for that width rather than a fixed size.
    const count = documentHandle.numPages;
    const bar = document.createElement("div");
    bar.className = "doc-barra";
    bar.textContent = `${count} ${count === 1 ? "página" : "páginas"}`;
    const list = document.createElement("div");
    list.className = "doc-lista";
    container.append(bar, list);
    const pageWidth = Math.max(1, Math.min(container.clientWidth || 850, 900));
    for (let number = 1; number <= count; number++) {
      const page = await documentHandle.getPage(number);
      const natural = page.getViewport({scale: 1});
      const viewport = page.getViewport({scale: Math.min(2, Math.max(0.5, pageWidth / natural.width))});
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("2d", {alpha: false});
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.ceil(viewport.width * ratio);
      canvas.height = Math.ceil(viewport.height * ratio);
      canvas.setAttribute("aria-label", `Página ${number} de ${count}`);
      const renderViewport = page.getViewport({scale: viewport.scale * ratio});
      await page.render({canvasContext: context, viewport: renderViewport}).promise;
      page.cleanup();
      const wrapper = document.createElement("section");
      wrapper.className = "doc-pagina";
      wrapper.append(canvas);
      list.append(wrapper);
      if (number % 4 === 0) await new Promise(resolve => setTimeout(resolve, 0));
    }
    onStatus(`${count} ${count === 1 ? "página" : "páginas"}.`);
    return () => {
      for (const canvas of list.querySelectorAll("canvas")) { canvas.width = 0; canvas.height = 0; }
      bar.remove();
      list.remove();
      release();
    };
  } catch (error) {
    container.replaceChildren();
    onStatus(error instanceof Error ? error.message : "Não foi possível exibir o PDF.", true);
    release();
    return () => release();
  }
}
