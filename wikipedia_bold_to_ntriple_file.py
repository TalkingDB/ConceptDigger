# Refer to redmine issue #4121 to know the purpose of this script

import time
import re
import dewiki

lookForBoldText=False
startingOfTextFound = False

#f = open('debug_data/Chiken_page_xml_on_wikipedia.txt','r')
f = open('enwiki-20141208-pages-articles.xml','r')
print "file opened"
p = open('bold_keywords.nt','wb')
PageURI =''
while True:
    line = f.readline()
    if line == '':break
    if lookForBoldText == False:#extract TITLE of page in this IF block
        if line[0:11] == "    <title>":
            PageURI = re.findall(">.*<",line)[0][1:-1]
            PageURI = PageURI.replace(" ", "_")
            lookForBoldText=True
            textOfPage = line
    else:
        if line[0:11] == "      <text":
            startingOfTextFound = True
        if startingOfTextFound == True:
            textOfPage = textOfPage  + line
            if ((line[-8:-1] == "</text>") or (line[:2]=="==")):
                startingOfTextFound = False
                lookForBoldText=False
                
                textOfPage = re.sub("{{[\s\S]*?}}", "", textOfPage, re.MULTILINE) #remove wiki markup code between the "{{ and }}" tags. Because those are often infoboxes which contain bold text but are not synonms
                textOfPage = re.sub("&lt;ref.*?&lt;\/ref","", textOfPage, re.MULTILINE)
                #entire contents of <text> xml tag has been found. now look for bold text in here
                boldKeywords = re.findall("'''.*?'''",textOfPage)
                for keyword in boldKeywords:
                    p.write('<http://dbpedia.org/resource/' + PageURI + '>    <http://www.w3.org/2000/01/rdf-schema#label>    "' + dewiki.from_string(keyword) +'"@en .\n')
                    #print('<http://dbpedia.org/resource/' + PageURI + '>    <http://www.w3.org/2000/01/rdf-schema#label>    "' + dewiki.from_string(keyword) +'"@en .\n')
                    
f.close()
p.close()