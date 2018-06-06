FROM informaticsmatters/rdkit-python-debian:Release_2018_03_02
ADD requirements.txt requirements.txt
RUN pip install -r requirements.txt
RUN apt-get update && apt-get install -y git
RUN git clone https://github.com/rdkit/mmpdb /usr/local/mmpdb
RUN pip install /usr/local/mmpdb 
ADD . /usr/local/fragalysis
RUN pip install /usr/local/fragalysis
