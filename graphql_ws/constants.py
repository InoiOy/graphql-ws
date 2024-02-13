## graphql-ws (Apollo)
# https://github.com/apollographql/subscriptions-transport-ws/blob/master/PROTOCOL.md
GRAPHQL_WS = "graphql-ws"

GQL_START = "start"  # Client -> Server
GQL_DATA = "data"  # Server -> Client
GQL_STOP = "stop"  # Client -> Server
GQL_CONNECTION_ERROR = "connection_error" # Server -> Client
GQL_CONNECTION_TERMINATE = "connection_terminate"  # Client -> Server
# NOTE: This one here don't follow the standard due to connection optimization
GQL_CONNECTION_KEEP_ALIVE = "ka"  # Server -> Client


## graphql-transport-ws
# https://github.com/enisdenjo/graphql-ws/blob/master/PROTOCOL.md
TRANSPORT_WS_PROTOCOL = "graphql-transport-ws"

GQL_SUBSCRIBE = "subscribe"  # Client -> Server
GQL_NEXT = "next"  # Server -> Client
GQL_PING = "ping"  # Bidirectional
GQL_PONG = "pong"  # Bidirectional


## Common messages for the two protocols
GQL_CONNECTION_INIT = "connection_init"  # Client -> Server
GQL_CONNECTION_ACK = "connection_ack"  # Server -> Client
GQL_ERROR = "error"  # Server -> Client
GQL_COMPLETE = "complete"  # Server -> Client (graphql-ws)
                           # Bidirectional (graphql-transport-ws)


# Default to Apollo graphql-ws
WS_PROTOCOL = GRAPHQL_WS
