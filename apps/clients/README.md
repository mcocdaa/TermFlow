# TermFlow Clients

TermFlow V1 intentionally contains no client implementation.

A future C (mobile app, web app, or desktop executable) authenticates only with B. It may list
online Instances, select existing Panes, render Base64 terminal bytes, and send ordinary text plus
optional Enter through the same V1 API. STT and a B-side Agent are outside V1; future automation
must use the authenticated control API and cannot bypass it to access A's private tmux socket.

Future mobile, web, and desktop clients will authenticate only with the Control Plane and use the
versioned protocol package. They will not connect directly to local tmux sockets.
