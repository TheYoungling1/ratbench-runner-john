FROM rust:1.82
WORKDIR /testbed
COPY . /testbed
RUN git init -q && git add -A && git -c user.email=t@t.co -c user.name=fixture commit -qm fixture
