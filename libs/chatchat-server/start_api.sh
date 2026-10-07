export CHATCHAT_AUTH_SECRET="$(openssl rand -hex 32)"
export CHATCHAT_ROOT=/home/lab239/chenzhuo/Langchain-Chatchat/chatchat-study-data

chatchat start --api
