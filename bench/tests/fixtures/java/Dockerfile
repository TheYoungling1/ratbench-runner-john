FROM maven:3.9-eclipse-temurin-17
WORKDIR /testbed
COPY . /testbed
RUN git init -q && git add -A && git -c user.email=t@t.co -c user.name=fixture commit -qm fixture
