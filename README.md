Concept Digger
====================

1. Takes input of DBPedia dump files (which were derived from Wikipedia)
2. Takes input of topics (wikipedia taxanomy of articles) from user
3. Looks for user's topics inside DBPedia dumps - extracts information about articles which lie under those topics, returns that information to client (generally Training Portal)


## Contributing ##

TODO : Occupies 10s of GBs of RAM as it tries to hold the vast wikipedia/dbpedia dataset in variables so that it can be queried quickly by clients. Consider some other approach through which we query the same vast data of wikipedia, while keeping data on hardisk (rather than in variables) but at the keeping our fast resopnse time


## LICENSE ##

This program has been made available in under the terms of the GNU Affero General Public License (AGPL). See individual files for details.
