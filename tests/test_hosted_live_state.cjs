'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { create, matches } = require('../static/js/hosted_chat_live_state');
const msg = (id, n = 1, status = 'queued') => ({id, created_at: `2026-10-03T00:00:0${n}+00:00`, updated_at: `2026-10-03T00:00:0${n}+00:00`, status});
const event = (message, operation = 'upsert') => ({operation, message, message_id: message.id, updated_at: message.updated_at});
test('an incoming delta survives an HTTP snapshot already in flight', () => {
 const state=create(), messages=new Map(), start=state.revision();
 state.apply(messages,event(msg('incoming')));
 state.merge(messages,{thread:[],has_more:false},start,false);
 assert.equal(messages.size,1);
});
test('an old HTTP status cannot overwrite a newer read receipt', () => {
 const state=create(), messages=new Map([['m',msg('m')]]), start=state.revision();
 state.apply(messages,event(msg('m',2,'read')));
 state.merge(messages,{thread:[msg('m',1,'sent')],has_more:false},start,false);
 assert.equal(messages.get('m').status,'read');
});
test('duplicate and out-of-order socket receipts do not duplicate or regress a bubble', () => {
 const state=create(), messages=new Map();
 assert.equal(state.apply(messages,event(msg('m',3,'read'))),true);
 assert.equal(state.apply(messages,event(msg('m',3,'read'))),false);
 assert.equal(state.apply(messages,event(msg('m',2,'sent'))),false);
 assert.equal(messages.size,1); assert.equal(messages.get('m').status,'read');
});
test('cancellation tombstones survive stale snapshots', () => {
 const state=create(), messages=new Map([['m',msg('m')]]), start=state.revision();
 state.apply(messages,event(msg('m',2),'remove'));
 state.merge(messages,{thread:[msg('m')],has_more:false},start,false);
 assert.equal(messages.size,0);
});
test('fresh authoritative snapshot removes drafts cancelled by bulk SQL', () => {
 const state=create(), messages=new Map([['m',msg('m')]]);
 state.merge(messages,{thread:[],has_more:false},state.revision(),false);
 assert.equal(messages.size,0);
});
test('latest page retains older history loaded explicitly', () => {
 const state=create(), messages=new Map([['old',msg('old',1)],['new',msg('new',3)]]);
 state.merge(messages,{thread:[msg('new',3)],has_more:true},state.revision(),false);
 assert.equal(messages.size,2);
});
test('older pagination never removes current live messages', () => {
 const state=create(), messages=new Map([['new',msg('new',3)]]);
 state.merge(messages,{thread:[msg('old',1)],has_more:false},state.revision(),true);
 assert.equal(messages.size,2);
});
test('LID aliases match only the selected conversation', () => {
 assert.equal(matches('42@lid',{chat_key:'+919999999999',aliases:['42@lid']}),true);
 assert.equal(matches('+918888888888',{chat_key:'+919999999999',aliases:['42@lid']}),false);
 assert.equal(matches('',{chat_key:''}),false);
});
test('a snapshot receipt cannot be downgraded by a delayed socket event', () => {
 const state=create(), messages=new Map();
 state.merge(messages,{thread:[msg('m',3,'read')],has_more:false},state.revision(),false);
 assert.equal(state.apply(messages,event(msg('m',2,'sent'))),false);
 assert.equal(messages.get('m').status,'read');
 assert.equal(state.apply(messages,event(msg('m',3,'read'))),false);
});
test('older paginated snapshots cannot overwrite a newer snapshot receipt', () => {
 const state=create(), messages=new Map();
 state.merge(messages,{thread:[msg('m',3,'read')],has_more:true},state.revision(),false);
 state.merge(messages,{thread:[msg('m',1,'queued')],has_more:false},state.revision(),true);
 assert.equal(messages.get('m').status,'read');
});
test('same-version snapshot may update bulk read state without losing socket fence', () => {
 const state=create(), messages=new Map();
 state.merge(messages,{thread:[{...msg('m',3,'received'),is_read:false}],has_more:false},state.revision(),false);
 state.merge(messages,{thread:[{...msg('m',3,'received'),is_read:true}],has_more:false},state.revision(),false);
 assert.equal(messages.get('m').is_read,true);
 assert.equal(state.apply(messages,event({...msg('m',2,'received'),is_read:false})),false);
});
