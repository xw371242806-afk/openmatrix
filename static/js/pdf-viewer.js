// OpenMatrix PDF viewer: PDF.js (Apache-2.0) rendering with citation highlighting.
// Defensive: any failure degrades to a visible message instead of throwing.

(function () {
    'use strict';

    var PDF_VIEWER_CONTAINER = 'pdfViewer';
    var PDF_VIEWER_STATUS = 'pdfViewerStatus';

    function safeElement(id) {
        return document.getElementById(id);
    }

    function setStatus(message, isError) {
        var status = safeElement(PDF_VIEWER_STATUS);
        if (!status) return;
        status.textContent = message || '';
        status.className = 'pdf-status ' + (isError ? 'pdf-status-error' : '');
    }

    function getPdfJs() {
        if (window.pdfjsLib) return window.pdfjsLib;
        return null;
    }

    function isPdfJsAvailable() {
        return typeof getPdfJs() !== 'undefined' && getPdfJs() !== null;
    }

    function clearViewer() {
        var container = safeElement(PDF_VIEWER_CONTAINER);
        if (container) container.innerHTML = '';
    }

    // Renders every page of the selected PDF into the viewer container.
    // quoteHighlighter is an optional callback(pdfTextByPage) invoked after load.
    function renderPdf(file, onReady) {
        if (!file) return;
        if (!isPdfJsAvailable()) {
            setStatus('PDF.js 未加载，无法预览文档。', true);
            return;
        }
        var container = safeElement(PDF_VIEWER_CONTAINER);
        if (!container) return;

        clearViewer();
        setStatus('正在加载 PDF...');
        var objectUrl = URL.createObjectURL(file);
        var pdfjsLib = getPdfJs();
        if (pdfjsLib.GlobalWorkerOptions && !pdfjsLib.GlobalWorkerOptions.workerSrc) {
            pdfjsLib.GlobalWorkerOptions.workerSrc = pdfjsLib.workerSrc || '';
        }

        pdfjsLib.getDocument({ url: objectUrl, isEvalSupported: false }).promise
            .then(function (pdf) {
                setStatus('共 ' + pdf.numPages + ' 页');
                var rendered = 0;
                var pages = [];
                for (var pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
                    (function (n) {
                        pdf.getPage(n).then(function (page) {
                            var wrap = document.createElement('div');
                            wrap.className = 'pdf-page';
                            var canvas = document.createElement('canvas');
                            var layer = document.createElement('div');
                            layer.className = 'pdf-text-layer';
                            wrap.appendChild(canvas);
                            wrap.appendChild(layer);
                            container.appendChild(wrap);

                            var viewport = page.getViewport({ scale: 1.4 });
                            canvas.width = viewport.width;
                            canvas.height = viewport.height;
                            page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport });

                            page.getTextContent().then(function (textContent) {
                                var text = '';
                                (textContent.items || []).forEach(function (item) {
                                    if (item && typeof item.str === 'string') text += item.str;
                                });
                                pages[n] = text;
                                rendered += 1;
                                if (rendered === pdf.numPages && typeof onReady === 'function') {
                                    onReady(pages);
                                }
                            });
                        }).catch(function () {
                            rendered += 1;
                            if (rendered === pdf.numPages && typeof onReady === 'function') {
                                onReady(pages);
                            }
                        });
                    })(pageNumber);
                }
            })
            .catch(function (err) {
                setStatus('PDF 加载失败：' + (err && err.message ? err.message : '未知错误'), true);
            })
            .finally(function () {
                setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 1000);
            });
    }

    // Highlights the first exact text match of quote across rendered pages.
    // Returns the page number (1-based) where a match was highlighted, or null.
    function highlightQuote(quote) {
        if (!quote) return null;
        var pages = Array.prototype.slice.call(document.querySelectorAll('.pdf-page'));
        for (var i = 0; i < pages.length; i++) {
            var layer = pages[i].querySelector('.pdf-text-layer');
            if (!layer) continue;
            var spans = layer.querySelectorAll('span');
            for (var s = 0; s < spans.length; s++) {
                var haystack = spans[s].textContent || '';
                if (haystack.indexOf(quote) !== -1) {
                    clearHighlights();
                    spans[s].className = 'pdf-highlight';
                    pages[i].scrollIntoView({ block: 'nearest' });
                    return i + 1;
                }
            }
        }
        return null;
    }

    function clearHighlights() {
        var highlighted = document.querySelectorAll('.pdf-highlight');
        Array.prototype.forEach.call(highlighted, function (el) {
            el.className = '';
        });
    }

    function highlightQuoteOnPage(quote, pageNumber) {
        if (!quote) return null;
        var pages = Array.prototype.slice.call(document.querySelectorAll('.pdf-page'));
        if (pageNumber == null || pageNumber < 1 || pageNumber > pages.length) {
            return highlightQuote(quote);
        }
        var layer = pages[pageNumber - 1].querySelector('.pdf-text-layer');
        if (!layer) return highlightQuote(quote);
        var spans = layer.querySelectorAll('span');
        for (var s = 0; s < spans.length; s++) {
            var haystack = spans[s].textContent || '';
            if (haystack.indexOf(quote) !== -1) {
                clearHighlights();
                spans[s].className = 'pdf-highlight';
                pages[pageNumber - 1].scrollIntoView({ block: 'nearest' });
                return pageNumber;
            }
        }
        return highlightQuote(quote);
    }

    // Citation navigator entry point: highlights the quote region and scrolls
    // the viewer to the cited page (1-based) when provided.
    function navigateToCitation(quote, pageNumber) {
        if (!quote) return null;
        var found = highlightQuoteOnPage(quote, pageNumber);
        if (found == null) {
            setStatus('未在 PDF 中找到引用原文：' + quote.slice(0, 60), true);
            return null;
        }
        return found;
    }

    window.OpenMatrixPdfViewer = {
        renderPdf: renderPdf,
        highlightQuote: highlightQuote,
        clearHighlights: clearHighlights,
        navigateToCitation: navigateToCitation,
        isAvailable: isPdfJsAvailable,
    };
})();
