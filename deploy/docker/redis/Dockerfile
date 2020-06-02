FROM redis:4.0.10

MAINTAINER MacArthur Lab

COPY bashrc /root/.bashrc

COPY redis.conf /usr/local/etc/redis/redis.conf

CMD [ "redis-server", "/usr/local/etc/redis/redis.conf" ]
