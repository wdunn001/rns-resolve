# +type: service
# +title: rns-resolve, name resolution for Reticulum
# +author: wdunn001
# +date: 2026-08-18
# +description: Look up human readable names and get ranked destination candidates, or register a name for a destination your own identity generates. Petnames first, resolver on miss, trust on first use.
# +tags: resolver, names, dns, petnames, rns-resolve, service
# +canonical: https://github.com/wdunn001/rns-resolve

>rns-resolve

Human readable names for Reticulum, without a registrar.

Reticulum addresses are 16 byte hashes. Nobody remembers them, and no
central authority exists to hand out names instead. rns-resolve fills the
gap the way the network itself works: names are evidence, not authority.

>>How to use this node

`[Look up a name`:/page/index.mu]

Type a name, get ranked candidates. Records registered here by their
owners come first, then names heard in network announces, each with its
evidence: expiry, trust score, last seen.

`[Register a name`:/page/register.mu]

Connect with identification enabled. The resolver derives the target
destination from your verified identity hash plus the app and aspects you
choose. You can only register names for destinations your own identity
generates, so nobody can point a name at a destination they do not
control. Registrations expire unless renewed by use.

Node operators: the intended flow is to register as part of setting your
node up, not by visiting this page. One command in your deploy reads your
node's identity and node_name and registers a signed, replicating record:
python -m rns_resolve.nodereg (see the repository). This page is the
manual path for everyone else.

>>The trust rules

A resolver is never asked about a real address. Anything shaped like a
32 character hash goes straight to the network, untouched; only input
that could never be a hash reaches a resolver. Answers are ranked
candidates, never one authoritative truth. Pin the hash you trust in
your own client, and after that your lookups are local, permanent, and
beyond any resolver's reach. A pinned name whose hash later changes is a
loud warning, exactly like an SSH host key change.

>>For your own client or service

The resolver is an ordinary Reticulum destination speaking msgpack
request and response, app rnsresolve, aspect query. Ops: resolve,
register, whois, plus a __manifest__ discovery op describing everything.
Registration over the wire is signed by your own key, so records
replicate between independent resolvers with no prior trust.

Source, protocol documentation, the command line client, and a NomadNet
browser patch that makes typed names resolve in place:

https://github.com/wdunn001/rns-resolve

>>Resolvers on this network

This node's resolver: 5f382b5d0f73a8e35adce587ef7f05f0
Peer resolver:        ca8751d6d24dcab3a7175264641954a5

Two independent resolvers, mutually replicating signed records. Run your
own: any node operator can, and the answers get better the more there
are.
