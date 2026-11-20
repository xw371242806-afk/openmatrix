# OpenMatrix Current Page Evidence MVP

This MV3 extension captures only the user-triggered active `http(s)` tab and
submits normalized readable text to a local OpenMatrix project.

## Load and test

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable developer mode and choose **Load unpacked**.
3. Select this `browser-extension` directory.
4. Start OpenMatrix locally and create a project in `/ui`.
5. Open an ordinary `http(s)` article, click the extension, enter the local
   OpenMatrix URL and project ID, then click **Capture current page**.
6. Verify the project document list contains the snapshot provenance URL.
7. Search for a unique captured phrase and verify the returned quote.

## Boundaries

- Uses `activeTab`; no `<all_urls>` permission and no background collection.
- Does not read cookies, local/session storage, password fields, form values,
  request bodies, or browser history.
- Does not perform login, navigation, form submission, downloads, or scraping.
- Keeps the optional OpenMatrix bearer token only in the popup's in-memory DOM;
  it is not written to extension storage.
- Sends only user-triggered current-page text, title, URL, canonical URL, and
  capture timestamp to the configured local OpenMatrix endpoint.
- The submitted page URL is client-asserted metadata; OpenMatrix does not
  fetch or independently verify it.
