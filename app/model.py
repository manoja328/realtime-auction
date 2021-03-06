import datetime
import logging

from google.appengine.ext import db

import paypal
import settings
import util

class Profile( db.Model ):
  owner = db.UserProperty()
  preapproval_amount = db.IntegerProperty() # cents
  preapproval_expiry = db.DateTimeProperty()
  preapproval_key = db.StringProperty()

  def amount_dollars( self ):
    return self.preapproval_amount / 100

  @staticmethod
  def find( user ):
    profile = Profile.all().filter( 'owner =', user ).get()
    if profile == None:
      profile = Profile( owner=user, preapproval_amount=0 )
      profile.save()
    return profile

class Item( db.Model ):
  created = db.DateTimeProperty(auto_now_add=True)
  title = db.StringProperty()
  image = db.BlobProperty()
  status = db.StringProperty( choices=( 'READY', 'INPROGRESS', 'FINISHED', 'SETTLED', 'DISABLED' ) )
  started = db.DateTimeProperty()

  def bid_info( self ):
    bids = Bid.all().filter( "item =", self ).order( "-amount" ).fetch(1)
    if len(bids) > 0:
      result = { 'bid': bids[0].amount_dollars(), 'bidder': bids[0].bidder.email() }
      last = bids[0].created
    else:
      result = { 'bid': '0.00', 'bidder': 'None' }
      last = self.started + datetime.timedelta( seconds=settings.BID_WAIT ) # double wait time for first bid
    no_bid_time = datetime.datetime.now() - last
    no_bid_time_seconds = no_bid_time.days * 86400 + no_bid_time.seconds
    result['remaining_s'] = settings.BID_WAIT - no_bid_time_seconds
    logging.info( "item %s with last %s has %i remaining" % ( self.title, last, result['remaining_s'] ) )
    return result

  def finished( self ):
    '''auction has finished'''
    bids = Bid.all().filter( "item =", self ).order( "-amount" ).fetch(1)
    if len(bids) > 0:
      self.status = 'FINISHED'
      if self.settle():
        # tell user
        bid = bids[0]
        util.notify_message( bid.bidder, 'STOP', 'You successfully purchased %s for $%.2f' % ( bid.item.title, bid.amount_dollars() ) )
      else:
        bid = bids[0]
        util.notify_message( bid.bidder, 'STOP', 'You won %s for $%.2f. Payment is required.' % ( bid.item.title, bid.amount_dollars() ) )
    else:
      self.status = 'READY'
    self.save()

  def settle( self ):
    '''auction has settled'''
    bid = Bid.all().filter( "item =", self ).order( "-amount" ).fetch(1)[0]
    profile = Profile.find( bid.bidder )
    # make the preapproved payment through paypal
    logging.info( "settling transaction..." )
    pay = paypal.PayWithPreapproval( amount=bid.amount_dollars(), preapproval_key=profile.preapproval_key )
    if pay.status() == 'COMPLETED':
      # update db
      profile.preapproval_amount -= bid.amount
      profile.save()
      self.status = 'SETTLED'
      self.save()
      logging.info( "settling transaction: done" )
      return True
    else:
      # something went wrong
      return False

  @staticmethod
  def state( message='' ):
    '''current state'''
    logging.info( "getting current item state" )
    item = Item.current()
    result = {}
    if item == None:
      item = Item.next()
      if item == None:
        result['message'] = 'No items available for auction. Try again later.'
        result['state'] = 'ERROR'
        return result
    else:
      bids = Bid.all().filter( "item =", item ).order( "-amount" ).fetch(1)

    bid_info = item.bid_info()
    if bid_info['remaining_s'] < 0:
      item.finished()
      Item.next()
      return Item.state( message ) # try again

    result['state'] = 'OK'
    result['bid'] = bid_info['bid']
    result['bidder'] = bid_info['bidder']
    result['key'] = str(item.key())
    result['item'] = item.title
    result['message'] = message
    result['remaining'] = '%i' % bid_info['remaining_s']

    return result

  @staticmethod
  def current():
    '''current auction'''
    return Item.all().filter( "status =", "INPROGRESS" ).get()

  @staticmethod
  def next():
    '''find a new item to sell'''
    item = Item.all().filter( "status =", "READY" ).order( "started" ).fetch(1)
    if len(item) > 0:
      logging.info( "setting %s to inprogress" % item[0].title )
      item[0].status = 'INPROGRESS'
      item[0].started = datetime.datetime.now()
      item[0].save()
      return item[0]

class Bid ( db.Model ):
  bidder = db.UserProperty()
  created = db.DateTimeProperty(auto_now_add=True)
  amount = db.IntegerProperty() # cents
  item = db.ReferenceProperty( Item )

  def amount_dollars( self ):
    return self.amount / 100.0

class Client( db.Model ):
  user = db.UserProperty()
  updated = db.DateTimeProperty(auto_now=True)

  @staticmethod
  def add( user ):
    # find and update or add new, remove old
    item = Client.all().filter( "user = ", user ).get()
    if item == None:
      Client( user=user ).save() 
    else:
      item.updated = datetime.datetime.now()
      item.save()

    # remove old
    too_old = datetime.datetime.now() - datetime.timedelta( seconds=600 )
    items = Client.all().filter( "updated <", too_old )
    for item in items:
      item.delete()

class Preapproval( db.Model ):
  '''track interaction with paypal'''
  user = db.UserProperty()
  created = db.DateTimeProperty(auto_now_add=True)
  status = db.StringProperty( choices=( 'NEW', 'CREATED', 'ERROR', 'CANCELLED', 'COMPLETED' ) )
  status_detail = db.StringProperty()
  secret = db.StringProperty() # to verify return_url
  debug_request = db.TextProperty()
  debug_response = db.TextProperty()
  preapproval_key = db.StringProperty()
  amount = db.IntegerProperty() # cents

