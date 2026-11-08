# Uploading EPUBs from Calibre to Readwise Books

The plugin uploads one selected EPUB at a time using the [Readwise Books uploads API](https://readwise.io/books_api#uploads). It sends the library's EPUB unchanged, under its Calibre file name. The same Readwise API token used for metadata and session sync authorizes the upload; there is no separate web sign-in.

The worker thread follows the documented flow:

1. `POST /books/api/v1/uploads` with the file name and exact byte size. The result supplies an upload id, a signed URL, and the headers for the file transfer.
2. Stream the EPUB file to the signed URL with a `PUT` and only the supplied headers. The API token never goes to file storage.
3. `POST /books/api/v1/uploads/complete` with the upload id and `currently_reading` shelf, matching the earlier uploader's behavior. Readwise processes the book asynchronously.
4. Poll `GET /books/api/v1/uploads?ids=<id>` for up to a minute. A ready result includes the created book. If it is still processing, the UI reports that state and refreshes the EPUBs tab later.

When a ready result includes a book id, the plugin saves that exact Calibre-to-Readwise link for the current Calibre library and Readwise token. The EPUBs tab can then recognize the upload even if Readwise's title or author differs from Calibre's metadata.

Readwise accepts EPUB files up to 500,000,000 bytes and limits upload starts to 10 per hour. The plugin does not automatically retry a failed completion request, since its outcome may be unknown. The error gives the upload id so the user can check Readwise before starting another upload.

The EPUBs tab lists each Calibre book with an EPUB and compares it with the Readwise Books library. It uses `list_books` and saved book links to show whether the book is an uploaded ebook, another Readwise book, deleted, or absent. A completed upload may take time to appear in that list.

The earlier implementation used undocumented web upload endpoints and saved web session cookies. The API flow replaces that code; no web cookies or embedded browser profile are needed.
